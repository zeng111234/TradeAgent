"""Customer management business logic."""
import math
from datetime import datetime, timedelta
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.customer import Customer, CustomerContact, CustomerNote, CustomerStage
from app.schemas.customer import (
    CustomerCreate, CustomerUpdate, CustomerContactCreate,
    CustomerNoteCreate, CustomerListResponse, CustomerResponse,
)


async def create_customer(db: AsyncSession, data: CustomerCreate) -> Customer:
    """Create a customer, and return it shaped the way the response needs.

    ``flush`` + ``refresh`` loads columns, not relationships. ``CustomerResponse``
    declares ``contacts`` and ``notes_list``, so the serialiser touches both --
    from outside the async session's greenlet context, where a lazy load raises
    ``MissingGreenlet``. The first test to call ``POST /customers`` with a new
    customer got a 500 instead of a 201, for a customer that had in fact been
    created. Re-reading with the eager loads the read path already uses makes
    the create path return the same shape as the get path.
    """
    customer = Customer(**data.model_dump())
    db.add(customer)
    await db.flush()

    loaded = await get_customer(db, customer.id)
    if loaded is None:  # pragma: no cover - just flushed, so it exists
        raise RuntimeError(f"customer {customer.id} disappeared after flush")
    return loaded


async def get_customer(db: AsyncSession, customer_id: int) -> Customer | None:
    """Get customer by ID with contacts and notes."""
    result = await db.execute(
        select(Customer)
        .options(selectinload(Customer.contacts), selectinload(Customer.notes_list))
        .where(Customer.id == customer_id)
    )
    return result.scalar_one_or_none()


async def list_customers(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 20,
    stage: str | None = None,
    source: str | None = None,
    country: str | None = None,
    search: str | None = None,
    tag: str | None = None,
) -> dict:
    """List customers with filtering and pagination."""
    query = select(Customer).options(
        selectinload(Customer.contacts),
        selectinload(Customer.notes_list),
    )

    # Filters
    if stage:
        query = query.where(Customer.stage == stage)
    if source:
        query = query.where(Customer.source == source)
    if country:
        query = query.where(Customer.country == country)
    if tag:
        query = query.where(Customer.tags.contains(tag))
    if search:
        query = query.where(
            or_(
                Customer.company_name.ilike(f"%{search}%"),
                Customer.company_name_cn.ilike(f"%{search}%"),
                Customer.industry.ilike(f"%{search}%"),
                Customer.products.ilike(f"%{search}%"),
            )
        )

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # Paginate
    offset = (page - 1) * page_size
    query = query.order_by(Customer.updated_at.desc()).offset(offset).limit(page_size)
    result = await db.execute(query)
    customers = result.scalars().all()

    return {
        "items": customers,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total > 0 else 0,
    }


async def update_customer(db: AsyncSession, customer_id: int, data: CustomerUpdate) -> Customer | None:
    """Update customer fields.

    ``expected_updated_at`` is the caller's concurrency guard, not a column: it
    is dropped here so it cannot be setattr'd onto the row.
    """
    customer = await get_customer(db, customer_id)
    if not customer:
        return None

    update_data = data.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    for field, value in update_data.items():
        setattr(customer, field, value)

    customer.updated_at = _next_version(customer.updated_at)
    await db.flush()
    await db.refresh(customer)
    return customer


def _next_version(previous: datetime | None) -> datetime:
    """A timestamp that is guaranteed to differ from the one before it.

    ``updated_at`` doubles as the optimistic-lock version: the router compares
    the value the caller loaded against the row's value and refuses the write
    when they differ. That comparison only works if a save actually moves the
    value, and ``datetime.utcnow()`` does not guarantee it.

    Measured on this machine: 19,996 of 20,000 consecutive ``utcnow()`` calls
    returned the *same* microsecond -- the system clock ticks at about 15.6 ms.
    Two people saving the same customer inside one tick therefore produced two
    identical versions, the second comparison passed, and the stale form was
    written over the fresh one. The 409 existed and never fired.

    Bumping by one microsecond keeps the column a datetime while making it
    strictly increasing, which is the only property the guard needs. It is not
    a clock: after a save inside one tick the timestamp is at most one
    microsecond ahead of real time, and it catches up as soon as the clock
    moves.
    """
    now = datetime.utcnow()
    if previous is not None and now <= previous:
        return previous + timedelta(microseconds=1)
    return now


async def delete_customer(db: AsyncSession, customer_id: int) -> bool:
    """Delete a customer."""
    customer = await get_customer(db, customer_id)
    if not customer:
        return False
    await db.delete(customer)
    await db.flush()
    return True


# --- Contacts ---
async def add_contact(db: AsyncSession, customer_id: int, data: CustomerContactCreate) -> CustomerContact | None:
    """Add a contact to a customer."""
    contact = CustomerContact(customer_id=customer_id, **data.model_dump())
    db.add(contact)
    await db.flush()
    await db.refresh(contact)
    return contact


async def get_contacts(db: AsyncSession, customer_id: int) -> list[CustomerContact]:
    """Get all contacts for a customer."""
    result = await db.execute(
        select(CustomerContact).where(CustomerContact.customer_id == customer_id)
    )
    return list(result.scalars().all())


# --- Notes ---
async def add_note(db: AsyncSession, customer_id: int, data: CustomerNoteCreate) -> CustomerNote:
    """Add a note to a customer."""
    note = CustomerNote(customer_id=customer_id, **data.model_dump())
    db.add(note)
    await db.flush()
    await db.refresh(note)
    return note


async def get_notes(db: AsyncSession, customer_id: int) -> list[CustomerNote]:
    """Get all notes for a customer, newest first."""
    result = await db.execute(
        select(CustomerNote)
        .where(CustomerNote.customer_id == customer_id)
        .order_by(CustomerNote.created_at.desc())
    )
    return list(result.scalars().all())


# --- Batch import ---
async def import_customers(db: AsyncSession, items: list[dict]) -> dict:
    """Import multiple customers from a list of dicts."""
    created = 0
    skipped = 0
    errors = []

    for item in items:
        try:
            company_name = item.get("company_name", "").strip()
            if not company_name:
                skipped += 1
                continue

            # Check if customer already exists
            existing = await db.execute(
                select(Customer).where(Customer.company_name == company_name)
            )
            if existing.scalar_one_or_none():
                skipped += 1
                continue

            customer = Customer(
                company_name=company_name,
                company_name_cn=item.get("company_name_cn"),
                country=item.get("country"),
                city=item.get("city"),
                website=item.get("website"),
                industry=item.get("industry"),
                products=item.get("products"),
                source="import",
            )
            db.add(customer)
            await db.flush()

            # Add contact if provided
            contact_name = item.get("contact_name", "").strip()
            if contact_name:
                contact = CustomerContact(
                    customer_id=customer.id,
                    name=contact_name,
                    email=item.get("contact_email"),
                    phone=item.get("contact_phone"),
                    is_primary=1,
                )
                db.add(contact)

            created += 1
        except Exception as e:
            errors.append(f"{item.get('company_name', 'unknown')}: {str(e)}")

    await db.flush()
    return {"created": created, "skipped": skipped, "errors": errors}