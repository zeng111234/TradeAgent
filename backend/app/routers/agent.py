"""AI Agent API routes - deep AI integration."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from app.services.agent_service import (
    analyze_customer_website,
    analyze_email_reply,
    get_negotiation_advice,
    daily_follow_up_intelligence,
    batch_personalize_emails,
)
from app.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

router = APIRouter(prefix="/agent", tags=["AI Agent"])


class WebsiteAnalysisRequest(BaseModel):
    url: str = Field(..., description="Customer website URL to analyze")
    your_products: str = Field("", description="Your products for matching analysis")


class EmailAnalysisRequest(BaseModel):
    email_content: str = Field(..., description="Customer email reply content to analyze")


class BatchEmailRequest(BaseModel):
    product_name: str = Field(..., description="Product you are selling")
    company_name: str = Field("", description="Your company name")
    selling_points: str = Field("", description="Key selling points")
    customer_ids: list[int] = Field([], description="Customer IDs to generate emails for")


class NegotiationRequest(BaseModel):
    customer_message: str = Field(..., description="What the customer said")
    product_name: str = Field(..., description="Product being negotiated")
    your_cost: float = Field(0, description="Your cost price (optional)")
    your_quote: float = Field(0, description="Your quoted price (optional)")
    context: str = Field("", description="Additional context (optional)")


@router.post("/analyze-website")
async def api_analyze_website(data: WebsiteAnalysisRequest):
    """Analyze a customer website to generate lead intelligence.
    
    Features:
    - Extracts company info, products, industry from website
    - Scores lead quality (0-100)
    - Recommends approach strategy
    - Identifies key selling points
    - Estimates order potential
    """
    if not data.url.startswith("http"):
        data.url = "https://" + data.url
    result = await analyze_customer_website(data.url, data.your_products)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/analyze-email")
async def api_analyze_email(data: EmailAnalysisRequest):
    """Analyze a customer email reply for actionable intelligence.
    
    Features:
    - Classifies intent (inquiry/price_negotiation/sample_request/rejection/order)
    - Detects sentiment and urgency
    - Extracts key info (quantity, budget, deadline)
    - Suggests next steps
    - Recommends reply content
    """
    return await analyze_email_reply(data.email_content)


@router.post("/daily-intelligence")
async def api_daily_intelligence(db: AsyncSession = Depends(get_db)):
    """AI Daily Intelligence - tells you who to follow up today.
    
    This is the CORE AI feature that replaces manual checking:
    - Reviews ALL your customers automatically
    - Prioritizes by urgency and deal stage
    - Generates draft follow-up emails for high-priority items
    - You just review and click send
    """
    return await daily_follow_up_intelligence(db)


@router.post("/batch-emails")
async def api_batch_emails(data: BatchEmailRequest, db: AsyncSession = Depends(get_db)):
    """Generate personalized emails for multiple customers at once.
    
    Instead of writing emails one by one, AI generates a unique email
    for each customer based on their industry, country, and products.
    """
    from sqlalchemy import select
    from app.models.customer import Customer, CustomerContact
    
    if not data.customer_ids:
        # Get all new/contacted customers
        result = await db.execute(
            select(Customer).where(Customer.stage.in_(["new", "contacted"])).limit(20)
        )
    else:
        result = await db.execute(
            select(Customer).where(Customer.id.in_(data.customer_ids))
        )
    
    customers = []
    for c in result.scalars().all():
        contact_result = await db.execute(
            select(CustomerContact)
            .where(CustomerContact.customer_id == c.id, CustomerContact.is_primary == 1)
            .limit(1)
        )
        contact = contact_result.scalar_one_or_none()
        customers.append({
            "id": c.id,
            "company_name": c.company_name,
            "country": c.country,
            "industry": c.industry,
            "products": c.products,
            "contact_email": contact.email if contact else None,
            "contact_name": contact.name if contact else None,
        })
    
    return await batch_personalize_emails(
        customers=customers,
        product_name=data.product_name,
        company_name=data.company_name,
        selling_points=data.selling_points,
    )


@router.post("/negotiation-advice")
async def api_negotiation_advice(data: NegotiationRequest):
    """AI negotiation copilot - get real-time strategies.
    
    Features:
    - Analyzes customer's true intent
    - Provides 3 negotiation strategies
    - Generates ready-to-send reply emails
    - Highlights pros/cons of each approach
    - Identifies red flags
    """
    return await get_negotiation_advice(
        customer_message=data.customer_message,
        product_name=data.product_name,
        your_cost=data.your_cost,
        your_quote=data.your_quote,
        context=data.context,
    )