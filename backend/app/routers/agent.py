"""AI Agent API routes - deep AI integration."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from app.services.agent_service import (
    analyze_customer_website,
    analyze_email_reply,
    get_negotiation_advice,
)

router = APIRouter(prefix="/agent", tags=["AI Agent"])


class WebsiteAnalysisRequest(BaseModel):
    url: str = Field(..., description="Customer website URL to analyze")
    your_products: str = Field("", description="Your products for matching analysis")


class EmailAnalysisRequest(BaseModel):
    email_content: str = Field(..., description="Customer email reply content to analyze")


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