"""Customer related schemas."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


# --- CustomerContact ---
class CustomerContactBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    whatsapp: Optional[str] = None
    linkedin: Optional[str] = None
    is_primary: int = 0
    notes: Optional[str] = None


class CustomerContactCreate(CustomerContactBase):
    pass


class CustomerContactUpdate(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    whatsapp: Optional[str] = None
    linkedin: Optional[str] = None
    is_primary: Optional[int] = None
    notes: Optional[str] = None


class CustomerContactResponse(CustomerContactBase):
    id: int
    customer_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# --- CustomerNote ---
class CustomerNoteBase(BaseModel):
    content: str = Field(..., min_length=1)
    note_type: str = "general"


class CustomerNoteCreate(CustomerNoteBase):
    pass


class CustomerNoteResponse(CustomerNoteBase):
    id: int
    customer_id: int
    created_at: datetime

    class Config:
        from_attributes = True


# --- Customer ---
class CustomerBase(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=200)
    company_name_cn: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    website: Optional[str] = None
    industry: Optional[str] = None
    products: Optional[str] = None
    source: str = "manual"
    stage: str = "new"
    score: float = 0.0
    tags: Optional[str] = None
    annual_import_value: Optional[str] = None
    notes: Optional[str] = None


class CustomerCreate(CustomerBase):
    pass


class CustomerUpdate(BaseModel):
    company_name: Optional[str] = None
    company_name_cn: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    website: Optional[str] = None
    industry: Optional[str] = None
    products: Optional[str] = None
    source: Optional[str] = None
    stage: Optional[str] = None
    score: Optional[float] = None
    tags: Optional[str] = None
    annual_import_value: Optional[str] = None
    notes: Optional[str] = None
    last_contacted_at: Optional[datetime] = None
    next_follow_up_at: Optional[datetime] = None


class CustomerResponse(CustomerBase):
    id: int
    created_at: datetime
    updated_at: datetime
    last_contacted_at: Optional[datetime] = None
    next_follow_up_at: Optional[datetime] = None
    contacts: list[CustomerContactResponse] = []
    notes_list: list[CustomerNoteResponse] = []

    class Config:
        from_attributes = True


class CustomerListResponse(BaseModel):
    """Paginated customer list."""
    items: list[CustomerResponse]
    total: int
    page: int
    page_size: int
    pages: int


class CustomerImportItem(BaseModel):
    """Single row for CSV/Excel import."""
    company_name: str
    company_name_cn: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    website: Optional[str] = None
    industry: Optional[str] = None
    products: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None