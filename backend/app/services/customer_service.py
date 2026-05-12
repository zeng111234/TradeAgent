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
    """Create a new customer."""
    customer = Customer(**data.model_dump())
    db.add(customer)
    await db.flush()
    await db.refresh(customer)
    return customer


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
    """Update customer fields."""
    customer = await get_customer(db, customer_id)
    if not customer:
        return None

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(customer, field, value)

    customer.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(customer)
    return customer


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