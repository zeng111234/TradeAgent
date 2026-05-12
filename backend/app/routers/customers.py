"""Customer management API routes."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

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
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


@router.put("/{customer_id}", response_model=CustomerResponse)
async def update_customer(
    customer_id: int, data: CustomerUpdate, db: AsyncSession = Depends(get_db)
):
    """Update a customer."""
    customer = await customer_service.update_customer(db, customer_id, data)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


@router.delete("/{customer_id}", status_code=204)
async def delete_customer(customer_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a customer."""
    success = await customer_service.delete_customer(db, customer_id)
    if not success:
        raise HTTPException(status_code=404, detail="Customer not found")


# --- Contacts ---
@router.post("/{customer_id}/contacts", response_model=CustomerContactResponse, status_code=201)
async def add_contact(
    customer_id: int, data: CustomerContactCreate, db: AsyncSession = Depends(get_db)
):
    """Add a contact to a customer."""
    contact = await customer_service.add_contact(db, customer_id, data)
    if not contact:
        raise HTTPException(status_code=404, detail="Customer not found")
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


# --- Import ---
@router.post("/import")
async def import_customers(
    items: list[CustomerImportItem], db: AsyncSession = Depends(get_db)
):
    """Batch import customers."""
    data = [item.model_dump() for item in items]
    result = await customer_service.import_customers(db, data)
    return result