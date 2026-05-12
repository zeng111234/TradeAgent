"""Email log schemas."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class EmailLogBase(BaseModel):
    to_email: str
    to_name: Optional[str] = None
    subject: str
    body: str


class EmailSendRequest(BaseModel):
    """Request to send a single email."""
    customer_id: Optional[int] = None
    contact_id: Optional[int] = None
    to_email: str = Field(..., description="Recipient email")
    to_name: Optional[str] = None
    template_id: Optional[int] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    # Template variables
    variables: Optional[dict] = None


class EmailBatchSendRequest(BaseModel):
    """Request to send emails to multiple recipients."""
    template_id: int
    customer_ids: list[int] = Field(..., description="List of customer IDs to send to")
    variables: Optional[dict] = None  # Additional variables


class EmailLogResponse(EmailLogBase):
    id: int
    customer_id: Optional[int] = None
    template_id: Optional[int] = None
    status: str
    tracking_id: Optional[str] = None
    opened_at: Optional[datetime] = None
    open_count: int
    replied_at: Optional[datetime] = None
    error_message: Optional[str] = None
    created_at: datetime
    sent_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class EmailStatsResponse(BaseModel):
    """Email statistics summary."""
    total_sent: int = 0
    total_opened: int = 0
    total_replied: int = 0
    total_bounced: int = 0
    total_failed: int = 0
    open_rate: float = 0.0
    reply_rate: float = 0.0