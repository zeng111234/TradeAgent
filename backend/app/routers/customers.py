"""Customer management API routes."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError, ErrorCode
from app.database import get_db
from app.schemas.customer import (
    CustomerCreate, CustomerUpdate, CustomerResponse,
    CustomerListResponse, CustomerContactCreate, CustomerContactResponse,
    CustomerNoteCreate, CustomerNoteResponse, CustomerImportItem,
)
from app.services import customer_service

router = APIRouter(prefix="/customers", tags=["Customers"])


@router.post("/", response_model=CustomerResponse, status_code=201)
async def create_customer(data: CustomerCreate, db: AsyncSession = Depends(get_db)):
    """Create a new customer."""
    customer = await customer_service.create_customer(db, data)
    return customer


@router.get("/", response_model=CustomerListResponse)
async def list_customers(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    stage: str | None = None,
    source: str | None = None,
    country: str | None = None,
    search: str | None = None,
    tag: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List customers with filtering and pagination."""
    return await customer_service.list_customers(
        db, page=page, page_size=page_size,
        stage=stage, source=source, country=country,
        search=search, tag=tag,
    )


@router.get("/{customer_id}", response_model=CustomerResponse)
async def get_customer(customer_id: int, db: AsyncSession = Depends(get_db)):
    """Get customer by ID."""
    customer = await customer_service.get_customer(db, customer_id)
    if not customer:
        raise ApiError(status_code=404, code=ErrorCode.CUSTOMER_NOT_FOUND, detail="Customer not found")
    return customer


@router.put("/{customer_id}", response_model=CustomerResponse)
async def update_customer(
    customer_id: int, data: CustomerUpdate, db: AsyncSession = Depends(get_db)
):
    """Update a customer.

    Optimistic concurrency, in the smallest possible form: the caller may send
    back the ``updated_at`` it loaded. If the row moved since, the write is
    refused with 409 and *nothing is changed*. Without the field the write goes
    through and the last one wins -- which is exactly what two people editing
    the same customer used to do to each other silently: whichever save landed
    second overwrote the first person's fields with a form loaded before them.
    Sending the timestamp is how a client says "I know what I am overwriting".
    """
    existing = await customer_service.get_customer(db, customer_id)
    if not existing:
        raise ApiError(status_code=404, code=ErrorCode.CUSTOMER_NOT_FOUND, detail="Customer not found")

    if data.expected_updated_at is not None and existing.updated_at != data.expected_updated_at:
        raise ApiError(
            status_code=409,
            code=ErrorCode.CUSTOMER_MODIFIED,
            detail=(
                f"this customer was changed at {existing.updated_at.isoformat()} "
                f"while you were editing (you loaded "
                f"{data.expected_updated_at.isoformat()}) -- nothing was changed. "
                "Reload the customer and reapply your edit, or your save would "
                "silently overwrite theirs."
            ),
            # The value a retry needs, so a client can merge and resubmit
            # without a round trip just to read it back. ``retryable`` is false
            # because *this* request will fail again; it becomes true once the
            # caller has the new timestamp.
            retryable=False,
            details={"current_updated_at": existing.updated_at.isoformat()},
        )

    customer = await customer_service.update_customer(db, customer_id, data)
    if not customer:
        raise ApiError(status_code=404, code=ErrorCode.CUSTOMER_NOT_FOUND, detail="Customer not found")
    return customer


@router.delete("/{customer_id}", status_code=204)
async def delete_customer(customer_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a customer that has no correspondence on file.

    Deleting a customer cascades to its email rows (``cascade="all,
    delete-orphan"``), and that is the hole this closes: a *sent* row is the
    record of what a customer actually received (see docs/问答记录.md R36), and
    the email log's own delete refuses to touch one (R39). Deleting the
    customer would destroy exactly those rows through the back door -- taking
    the evidence with it, while the guard on the row itself says they are kept
    on purpose. So the customer can go only when everything on file is still a
    draft: proposals travel with the customer, correspondence stays.
    """
    from sqlalchemy import func, select

    from app.models.email_log import EmailLog, EmailStatus

    existing = await customer_service.get_customer(db, customer_id)
    if not existing:
        raise ApiError(status_code=404, code=ErrorCode.CUSTOMER_NOT_FOUND, detail="Customer not found")

    kept = (
        await db.execute(
            select(func.count(EmailLog.id)).where(
                EmailLog.customer_id == customer_id,
                EmailLog.status != EmailStatus.PENDING,
            )
        )
    ).scalar_one()
    if kept:
        raise ApiError(
            status_code=409,
            code=ErrorCode.CUSTOMER_HAS_MAIL,
            detail=(
                f"this customer has {kept} email record(s) that were sent, "
                "attempted or decided -- those rows are the record of what went "
                "out and cannot be deleted (the same rule as the email log "
                "itself), so nothing was deleted. Drafts travel with the "
                "customer; the correspondence stays on purpose."
            ),
        )

    success = await customer_service.delete_customer(db, customer_id)
    if not success:
        raise ApiError(status_code=404, code=ErrorCode.CUSTOMER_NOT_FOUND, detail="Customer not found")


# --- Contacts ---
@router.post("/{customer_id}/contacts", response_model=CustomerContactResponse, status_code=201)
async def add_contact(
    customer_id: int, data: CustomerContactCreate, db: AsyncSession = Depends(get_db)
):
    """Add a contact to a customer."""
    contact = await customer_service.add_contact(db, customer_id, data)
    if not contact:
        raise ApiError(status_code=404, code=ErrorCode.CUSTOMER_NOT_FOUND, detail="Customer not found")
    return contact


@router.get("/{customer_id}/contacts", response_model=list[CustomerContactResponse])
async def get_contacts(customer_id: int, db: AsyncSession = Depends(get_db)):
    """Get all contacts for a customer."""
    return await customer_service.get_contacts(db, customer_id)


# --- Notes ---
@router.post("/{customer_id}/notes", response_model=CustomerNoteResponse, status_code=201)
async def add_note(
    customer_id: int, data: CustomerNoteCreate, db: AsyncSession = Depends(get_db)
):
    """Add a note to a customer."""
    return await customer_service.add_note(db, customer_id, data)


@router.get("/{customer_id}/notes", response_model=list[CustomerNoteResponse])
async def get_notes(customer_id: int, db: AsyncSession = Depends(get_db)):
    """Get all notes for a customer."""
    return await customer_service.get_notes(db, customer_id)


# --- Debug ---
@router.get("/{customer_id}/debug")
async def debug_customer(customer_id: int, db: AsyncSession = Depends(get_db)):
    """Everything needed to answer "what happened to this customer, and why".

    Read-only, and deliberately so: the POST form of the follow-up plan *writes*
    the commitments it computes, so investigating with it would change the thing
    under investigation. This builds the plan with ``persist=False``.

    The interesting half is ``findings``: the same facts, in plain language,
    most likely cause first. The rest is the evidence behind them.
    """
    from app.services.customer_debug import debug_customer as build_debug_view

    view = await build_debug_view(db, customer_id)
    if view is None:
        raise ApiError(status_code=404, code=ErrorCode.CUSTOMER_NOT_FOUND, detail="Customer not found")
    return view


# --- Import ---
@router.post("/import")
async def import_customers(
    items: list[CustomerImportItem], db: AsyncSession = Depends(get_db)
):
    """Batch import customers."""
    data = [item.model_dump() for item in items]
    result = await customer_service.import_customers(db, data)
    return result