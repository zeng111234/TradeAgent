"""Email template schemas."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class EmailTemplateBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    subject: str = Field(..., min_length=1, max_length=500)
    body: str = Field(..., min_length=1)
    language: str = "en"
    category: Optional[str] = None
    is_ai_generated: int = 0


class EmailTemplateCreate(EmailTemplateBase):
    pass


class EmailTemplateUpdate(BaseModel):
    name: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    language: Optional[str] = None
    category: Optional[str] = None


class EmailTemplateResponse(EmailTemplateBase):
    id: int
    use_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AIGenerateRequest(BaseModel):
    """Request to generate email content via AI."""
    product_name: str = Field(..., description="Product you are selling")
    target_industry: Optional[str] = Field(None, description="Target buyer industry")
    target_country: Optional[str] = Field(None, description="Target buyer country")
    company_name: Optional[str] = Field(None, description="Your company name")
    selling_points: Optional[str] = Field(None, description="Key selling points")
    tone: str = Field("professional", description="Tone: professional, friendly, casual")
    language: str = Field("en", description="Output language")


class AIGenerateResponse(BaseModel):
    """AI generated email response."""
    subject: str
    body: str
    suggestions: Optional[list[str]] = None