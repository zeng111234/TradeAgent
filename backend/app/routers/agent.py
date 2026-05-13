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
    analyze_inquiry,
    customer_churn_alerts,
    generate_holiday_emails,
    get_holiday_calendar,
    auto_lead_scanner,
    generate_pi,
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


# --- New Feature 1: Inquiry Analysis ---

class InquiryAnalysisRequest(BaseModel):
    email_content: str = Field(..., description="Customer inquiry email content")


@router.post("/analyze-inquiry")
async def api_analyze_inquiry(data: InquiryAnalysisRequest):
    """AI inquiry analysis - parse customer inquiry into structured data.
    
    Replaces manual reading and note-taking:
    - Extracts product names, specs, quantities
    - Detects shipping terms (FOB/CIF/EXW)
    - Scores customer intent (0-100)
    - Generates a draft reply email
    - Suggests follow-up strategy
    """
    return await analyze_inquiry(data.email_content)


# --- New Feature 2: Customer Churn Alerts ---

@router.post("/churn-alerts")
async def api_churn_alerts(db: AsyncSession = Depends(get_db)):
    """AI customer churn alert system - detect at-risk customers.
    
    Replaces the manual process of checking each customer:
    - Scans ALL customers automatically
    - Detects 5 types of risk (no contact, no reply, cold leads, stuck stages, unread quotes)
    - Classifies risk as critical/high/medium
    - Generates AI rescue emails for critical customers
    - One-click send rescue emails
    """
    return await customer_churn_alerts(db)


# --- New Feature 3: Holiday Emails ---

class HolidayEmailRequest(BaseModel):
    holiday_name: str = Field(..., description="Holiday name (e.g. Christmas, New Year)")
    company_name: str = Field("", description="Your company name")
    custom_message: str = Field("", description="Custom message to include")


@router.get("/holidays")
async def api_get_holidays():
    """Get holiday calendar with upcoming holidays highlighted.
    
    Shows all major international trade holidays with:
    - Days until the holiday
    - Which markets it applies to
    - Whether it's upcoming (within 30 days)
    """
    return get_holiday_calendar()


@router.post("/holiday-emails")
async def api_generate_holiday_emails(data: HolidayEmailRequest, db: AsyncSession = Depends(get_db)):
    """AI generates personalized holiday greeting emails for all customers.
    
    Instead of manually sending holiday greetings one by one:
    1. Select a holiday from the calendar
    2. AI scans all active customers
    3. Generates personalized greeting based on customer's country/industry
    4. Preview all emails and one-click send
    """
    return await generate_holiday_emails(
        holiday_name=data.holiday_name,
        company_name=data.company_name,
        custom_message=data.custom_message,
        db_session=db,
    )


# --- New Feature 4: Auto Lead Scanner ---

class LeadScanRequest(BaseModel):
    product_keywords: str = Field(..., description="Product keywords (e.g. gold thread, metallic yarn)")
    target_country: str = Field("", description="Target country (e.g. Germany, USA)")
    target_region: str = Field("", description="Target region/state (optional)")
    max_results: int = Field(10, description="Max number of results", ge=1, le=20)


@router.post("/scan-leads")
async def api_scan_leads(data: LeadScanRequest):
    """Auto Lead Scanner - find potential buyers automatically.
    
    Instead of manually Googling and visiting sites:
    1. Enter product keywords + target country
    2. Agent searches Google, visits each result
    3. Extracts company name, email, phone from each site
    4. Scores relevance
    5. AI enriches with priority and approach advice
    6. One-click import to CRM
    """
    return await auto_lead_scanner(
        product_keywords=data.product_keywords,
        target_country=data.target_country,
        target_region=data.target_region,
        max_results=data.max_results,
    )


# --- New Feature 5: PI Generator ---

class ProductItem(BaseModel):
    name: str = Field(..., description="Product name")
    spec: str = Field("", description="Specification")
    qty: float = Field(..., description="Quantity")
    unit: str = Field("pcs", description="Unit (pcs/rolls/meters/kg)")
    unit_price: float = Field(..., description="Unit price in USD")


class PIRequest(BaseModel):
    customer_name: str = Field(..., description="Customer contact name")
    customer_company: str = Field(..., description="Customer company name")
    customer_address: str = Field("", description="Customer address")
    customer_email: str = Field("", description="Customer email")
    products: list[ProductItem] = Field(..., description="Product list")
    trade_terms: str = Field("FOB", description="Trade terms (FOB/CIF/EXW)")
    payment_terms: str = Field("T/T 30% deposit, 70% before shipment", description="Payment terms")
    validity_days: int = Field(15, description="PI validity in days")
    your_company: str = Field("", description="Your company name")
    your_address: str = Field("", description="Your company address")
    notes: str = Field("", description="Additional notes")


@router.post("/generate-pi")
async def api_generate_pi(data: PIRequest):
    """PI (Proforma Invoice) Generator.
    
    Instead of manually creating PI in Word/Excel:
    1. Enter customer info and product list
    2. Auto-generates professional PI with proper formatting
    3. Auto-calculates totals
    4. Returns print-ready HTML
    5. Can be emailed directly to customer
    """
    return await generate_pi(
        customer_name=data.customer_name,
        customer_company=data.customer_company,
        customer_address=data.customer_address,
        customer_email=data.customer_email,
        products=[p.dict() for p in data.products],
        trade_terms=data.trade_terms,
        payment_terms=data.payment_terms,
        validity_days=data.validity_days,
        your_company=data.your_company,
        your_address=data.your_address,
        notes=data.notes,
    )
