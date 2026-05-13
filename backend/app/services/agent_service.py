"""AI Agent service - deep AI integration for foreign trade."""
import logging
import json
import re
from app.config import settings

logger = logging.getLogger(__name__)


async def analyze_customer_website(url: str, your_products: str = "") -> dict:
    """Analyze a customer's website to generate lead intelligence.
    
    Steps:
    1. Fetch and extract text from the website
    2. Use AI to analyze the company
    3. Score the lead and provide recommendations
    """
    import requests
    from bs4 import BeautifulSoup

    # Step 1: Fetch website content
    try:
        resp = requests.get(url, timeout=15, verify=False, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        soup = BeautifulSoup(resp.text, "lxml")
        # Remove scripts and styles
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        page_text = soup.get_text(separator="\n", strip=True)[:3000]
        title = soup.title.string if soup.title else ""
    except Exception as e:
        return {"error": f"Failed to fetch website: {str(e)}", "score": 0}

    # Step 2: AI Analysis
    if settings.OPENAI_API_KEY:
        return await _ai_analyze_website(page_text, title, url, your_products)
    else:
        return _basic_analyze_website(page_text, title, url)


async def _ai_analyze_website(page_text: str, title: str, url: str, your_products: str) -> dict:
    """Use LLM to deeply analyze a website."""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL)

        prompt = f"""You are an expert foreign trade analyst. Analyze this potential buyer's website and provide a detailed lead assessment.

Website URL: {url}
Website Title: {title}
Website Content (first 3000 chars):
{page_text}

My products: {your_products or "Not specified"}

Please analyze and return a JSON object with these fields:
{{
    "company_name": "extracted company name",
    "country": "detected country",
    "industry": "detected industry",
    "products_they_sell": ["list of products they sell or import"],
    "company_size_estimate": "small/medium/large",
    "likely_buying_interest": ["products they might want to buy from us"],
    "lead_score": 0-100,
    "score_reasoning": "why this score",
    "recommended_approach": "how to approach this customer",
    "key_selling_points": ["what to highlight when contacting them"],
    "best_contact_angle": "the best angle to start a conversation",
    "estimated_order_potential": "small/medium/large",
    "risk_factors": ["potential risks or concerns"]
}}

Return ONLY valid JSON, no other text."""

        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a B2B trade intelligence analyst. Always respond with valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=1500,
        )

        content = response.choices[0].message.content.strip()
        # Try to parse JSON
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        result = json.loads(content)
        result["ai_powered"] = True
        result["source_url"] = url
        return result

    except json.JSONDecodeError:
        logger.error("AI returned non-JSON response")
        return _basic_analyze_website(page_text, title, url)
    except Exception as e:
        logger.error(f"AI analysis error: {e}")
        return _basic_analyze_website(page_text, title, url)


def _basic_analyze_website(page_text: str, title: str, url: str) -> dict:
    """Basic keyword-based website analysis (fallback)."""
    text_lower = page_text.lower()

    # Detect industry
    industry_keywords = {
        "electronics": ["electronics", "led", "solar", "battery", "charger", "cable", "circuit"],
        "home_garden": ["furniture", "home", "garden", "kitchen", "decor", "lighting"],
        "automotive": ["automotive", "auto parts", "vehicle", "car", "engine"],
        "textile": ["textile", "clothing", "fabric", "apparel", "garment"],
        "food": ["food", "beverage", "snack", "organic", "drink"],
        "machinery": ["machinery", "equipment", "industrial", "manufacturing"],
        "packaging": ["packaging", "box", "bag", "label", "container"],
    }

    detected_industry = "unknown"
    for ind, keywords in industry_keywords.items():
        if any(kw in text_lower for kw in keywords):
            detected_industry = ind
            break

    # Detect country
    country_clues = {
        "United States": [".com", "usa", "america", "united states"],
        "Germany": [".de", "germany", "gmbh"],
        "United Kingdom": [".co.uk", "uk", "ltd", "britain"],
        "Japan": [".jp", "japan", "co.jp"],
        "Spain": [".es", "spain"],
        "Brazil": [".br", "brazil"],
    }
    detected_country = "unknown"
    url_lower = url.lower()
    for country, clues in country_clues.items():
        if any(clue in url_lower or clue in text_lower for clue in clues):
            detected_country = country
            break

    # Simple scoring
    score = 40  # base
    if detected_industry != "unknown":
        score += 15
    if detected_country != "unknown":
        score += 10
    if "import" in text_lower or "wholesale" in text_lower or "distributor" in text_lower:
        score += 20
    if "contact" in text_lower or "inquiry" in text_lower:
        score += 10

    return {
        "company_name": title.split("-")[0].strip() if title else "Unknown",
        "country": detected_country,
        "industry": detected_industry,
        "products_they_sell": [],
        "company_size_estimate": "unknown",
        "likely_buying_interest": [],
        "lead_score": min(score, 100),
        "score_reasoning": "Basic keyword analysis. Add API key for deeper AI analysis.",
        "recommended_approach": "Send a professional cold email introducing your products.",
        "key_selling_points": [],
        "best_contact_angle": "General product inquiry",
        "estimated_order_potential": "unknown",
        "risk_factors": [],
        "ai_powered": False,
        "source_url": url,
    }


async def analyze_email_reply(email_content: str) -> dict:
    """Analyze a customer's email reply and provide actionable insights.
    
    Classifies intent, extracts key info, suggests next steps.
    """
    if not settings.OPENAI_API_KEY:
        return _basic_reply_analysis(email_content)

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL)

        prompt = f"""You are an expert foreign trade email analyst. Analyze this customer reply and extract actionable intelligence.

Customer Email:
{email_content}

Return a JSON object:
{{
    "intent": "inquiry|price_negotiation|sample_request|rejection|order|info_request|complaint|follow_up",
    "sentiment": "positive|neutral|negative",
    "urgency": "high|medium|low",
    "extracted_info": {{
        "products_mentioned": [],
        "quantity_mentioned": "",
        "budget_mentioned": "",
        "delivery_deadline": "",
        "target_price": "",
        "other_requirements": []
    }},
    "customer_mood": "description of how the customer seems to feel",
    "recommended_action": "specific next step recommendation",
    "suggested_reply_points": ["key points to include in your reply"],
    "deal_stage_update": "prospect|warm|hot|closing|lost",
    "risks": ["potential concerns to address"]
}}

Return ONLY valid JSON."""

        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a B2B trade email analyst. Always respond with valid JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=1000,
        )

        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        result = json.loads(content)
        result["ai_powered"] = True
        return result

    except Exception as e:
        logger.error(f"Email analysis error: {e}")
        return _basic_reply_analysis(email_content)


def _basic_reply_analysis(email_content: str) -> dict:
    """Basic keyword-based reply analysis."""
    text_lower = email_content.lower()

    intent = "follow_up"
    if any(w in text_lower for w in ["price", "cost", "how much", "quote", "discount"]):
        intent = "price_negotiation"
    elif any(w in text_lower for w in ["sample", "trial", "test"]):
        intent = "sample_request"
    elif any(w in text_lower for w in ["not interested", "no thanks", "decline"]):
        intent = "rejection"
    elif any(w in text_lower for w in ["order", "purchase", "buy", "place order"]):
        intent = "order"
    elif any(w in text_lower for w in ["interested", "tell me more", "catalog"]):
        intent = "inquiry"

    sentiment = "neutral"
    if any(w in text_lower for w in ["great", "good", "interested", "perfect", "excellent"]):
        sentiment = "positive"
    elif any(w in text_lower for w in ["unfortunately", "not interested", "too expensive", "poor"]):
        sentiment = "negative"

    return {
        "intent": intent,
        "sentiment": sentiment,
        "urgency": "medium",
        "extracted_info": {"products_mentioned": [], "quantity_mentioned": "", "budget_mentioned": ""},
        "customer_mood": "Neutral",
        "recommended_action": "Follow up with more product details",
        "suggested_reply_points": ["Thank them for their reply", "Address their specific questions"],
        "deal_stage_update": "warm" if intent in ("inquiry", "sample_request") else "prospect",
        "risks": [],
        "ai_powered": False,
    }


async def daily_follow_up_intelligence(db_session=None) -> dict:
    """AI reviews ALL your customers and tells you exactly who to follow up today.
    
    This is the CORE differentiator - instead of you checking each customer,
    AI analyzes everyone and gives you a prioritized action list.
    """
    if not db_session:
        return {"error": "Database session required", "actions": []}

    from sqlalchemy import select, or_, and_
    from app.models.customer import Customer, CustomerContact
    from app.models.email_log import EmailLog, EmailStatus
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    actions = []

    # 1. Find customers that need follow-up (contacted but no reply in 3+ days)
    result = await db_session.execute(
        select(Customer).where(
            Customer.stage.in_(["contacted", "interested", "quoting"])
        )
    )
    customers = result.scalars().all()

    for c in customers:
        # Check last email
        email_result = await db_session.execute(
            select(EmailLog)
            .where(EmailLog.customer_id == c.id)
            .order_by(EmailLog.created_at.desc())
            .limit(1)
        )
        last_email = email_result.scalar_one_or_none()

        priority = "low"
        reason = ""
        suggested_action = ""

        if c.stage == "quoting":
            priority = "high"
            reason = f"In quoting stage - follow up on quote for {c.company_name}"
            suggested_action = "Follow up on quoted price, ask if they need samples"
        elif c.stage == "interested":
            priority = "medium"
            reason = f"Customer showed interest - keep momentum"
            suggested_action = "Send product details or case studies"
        elif c.stage == "contacted":
            if last_email and (now - last_email.created_at).days >= 3:
                priority = "high"
                reason = f"No reply in {(now - last_email.created_at).days} days"
                suggested_action = "Send a follow-up email with different angle"
            elif not last_email:
                priority = "medium"
                reason = f"Marked as contacted but no email record"
                suggested_action = "Send initial outreach email"
            else:
                priority = "low"
                reason = f"Recently contacted, wait for reply"
                suggested_action = "Wait 2 more days before follow-up"

        if reason:
            # Get primary contact
            contact_result = await db_session.execute(
                select(CustomerContact)
                .where(CustomerContact.customer_id == c.id, CustomerContact.is_primary == 1)
                .limit(1)
            )
            contact = contact_result.scalar_one_or_none()

            actions.append({
                "customer_id": c.id,
                "company_name": c.company_name,
                "stage": c.stage,
                "priority": priority,
                "reason": reason,
                "suggested_action": suggested_action,
                "contact_email": contact.email if contact else None,
                "contact_name": contact.name if contact else None,
                "last_contacted": last_email.created_at.isoformat() if last_email else None,
            })

    # Sort by priority
    priority_order = {"high": 0, "medium": 1, "low": 2}
    actions.sort(key=lambda x: priority_order.get(x["priority"], 3))

    # Generate one-click follow-up emails for high priority
    if settings.OPENAI_API_KEY and actions:
        for action in actions[:5]:  # Top 5 high priority
            if action["priority"] == "high" and action["contact_email"]:
                try:
                    email_result = await db_session.execute(
                        select(EmailLog)
                        .where(EmailLog.customer_id == action["customer_id"])
                        .order_by(EmailLog.created_at.desc())
                        .limit(1)
                    )
                    last = email_result.scalar_one_or_none()
                    last_subject = last.subject if last else "initial outreach"

                    from openai import AsyncOpenAI
                    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL)

                    prompt = f"""Write a short follow-up email (under 100 words) for a foreign trade business.
Company: {action['company_name']}
Contact: {action['contact_name'] or 'Sir/Madam'}
Last email subject: {last_subject}
Current stage: {action['stage']}
Reason: {action['reason']}

Write a warm, professional follow-up. Be specific to their situation. Return ONLY the email body (HTML)."""

                    resp = await client.chat.completions.create(
                        model=settings.OPENAI_MODEL,
                        messages=[
                            {"role": "system", "content": "You are a professional foreign trade email writer. Write concise, warm emails."},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.7,
                        max_tokens=300,
                    )
                    action["draft_email"] = resp.choices[0].message.content.strip()
                    action["draft_subject"] = f"Following up - {action['company_name']}"
                except Exception as e:
                    logger.error(f"Draft email error: {e}")

    return {
        "total_actions": len(actions),
        "high_priority": len([a for a in actions if a["priority"] == "high"]),
        "medium_priority": len([a for a in actions if a["priority"] == "medium"]),
        "actions": actions,
    }


async def batch_personalize_emails(
    customers: list[dict],
    product_name: str,
    company_name: str = "",
    selling_points: str = "",
) -> dict:
    """Generate personalized emails for ALL customers at once.
    
    Instead of writing one email at a time, AI generates a unique email
    for each customer based on their industry, country, and products.
    This is the killer feature for batch outreach.
    """
    if not settings.OPENAI_API_KEY:
        return {"error": "API key required for batch email generation", "emails": []}

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL)

    emails = []
    for cust in customers:
        try:
            prompt = f"""Write a personalized cold outreach email for this buyer.
Your company: {company_name or 'Our company'}
Your product: {product_name}
Selling points: {selling_points or 'High quality, competitive price'}

Buyer info:
- Company: {cust.get('company_name', 'Unknown')}
- Country: {cust.get('country', 'Unknown')}
- Industry: {cust.get('industry', 'Unknown')}
- Products they sell: {cust.get('products', 'Unknown')}

Requirements:
- Reference their specific industry/country/products
- Explain WHY your product is relevant to THEM specifically
- Keep under 120 words
- Professional but warm tone

Return JSON: {{"subject": "email subject", "body": "email body in HTML"}}"""

            resp = await client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "Write B2B cold outreach emails. Return valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                max_tokens=400,
            )

            content = resp.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            result = json.loads(content)

            emails.append({
                "customer_id": cust.get("id"),
                "company_name": cust.get("company_name"),
                "to_email": cust.get("contact_email"),
                "subject": result.get("subject", f"Inquiry about {product_name}"),
                "body": result.get("body", ""),
                "personalized_for": f"{cust.get('country', '')} / {cust.get('industry', '')}",
            })
        except Exception as e:
            logger.error(f"Batch email error for {cust.get('company_name')}: {e}")
            emails.append({
                "customer_id": cust.get("id"),
                "company_name": cust.get("company_name"),
                "error": str(e),
            })

    return {
        "total": len(customers),
        "generated": len([e for e in emails if "error" not in e]),
        "failed": len([e for e in emails if "error" in e]),
        "emails": emails,
    }


async def get_negotiation_advice(
    customer_message: str,
    product_name: str,
    your_cost: float = 0,
    your_quote: float = 0,
    context: str = "",
) -> dict:
    """AI negotiation copilot - get real-time advice during price negotiations."""
    if not settings.OPENAI_API_KEY:
        return {
            "strategies": [
                {"type": "stand_firm", "reply": "Thank you for your feedback. Our price reflects the premium quality and after-sales service we provide."},
                {"type": "offer_value", "reply": "While we can't lower the price, we can offer extended warranty and free samples for your next order."},
                {"type": "meet_halfway", "reply": "We value our partnership. For this order, we can offer a 5% discount if the quantity exceeds 1000 units."},
            ],
            "ai_powered": False,
        }

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL)

        prompt = f"""You are an expert B2B negotiation coach for a Chinese foreign trade company.

Customer said: "{customer_message}"
Product: {product_name}
{'My cost: $' + str(your_cost) if your_cost else ''}
{'My quoted price: $' + str(your_quote) if your_quote else ''}
{'Additional context: ' + context if context else ''}

Provide 3 negotiation strategies, each with a ready-to-send reply email.

Return JSON:
{{
    "customer_intent_analysis": "what the customer really wants",
    "strategies": [
        {{
            "type": "stand_firm|offer_value|meet_halfway|bundle_deal|volume_discount",
            "name": "strategy name",
            "reply": "ready to send email reply in English",
            "pros": "why this might work",
            "cons": "risk of this approach"
        }},
        {{...}},
        {{...}}
    ],
    "recommended_strategy": "which one you recommend and why",
    "talking_points": ["key points to remember during negotiation"],
    "red_flags": ["things to watch out for"]
}}

Return ONLY valid JSON."""

        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are an expert B2B negotiation coach. Always respond with valid JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            max_tokens=2000,
        )

        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        result = json.loads(content)
        result["ai_powered"] = True
        return result

    except Exception as e:
        logger.error(f"Negotiation advice error: {e}")
        return {
            "customer_intent_analysis": "Customer wants a lower price",
            "strategies": [
                {"type": "stand_firm", "name": "Hold Price", "reply": "Thank you for your feedback. Our price reflects premium quality.", "pros": "Maintains margins", "cons": "May lose the deal"},
                {"type": "offer_value", "name": "Add Value", "reply": "We can't reduce the price, but we can offer free samples and priority shipping.", "pros": "Adds value without cutting price", "cons": "Customer may still want lower price"},
                {"type": "meet_halfway", "name": "Small Discount", "reply": "For this order, we offer 5% discount for quantities over 1000 units.", "pros": "Shows flexibility", "cons": "Reduces margin"},
            ],
            "recommended_strategy": "Start with offer_value, then negotiate to meet_halfway if needed",
            "ai_powered": False,
        }


async def analyze_inquiry(email_content: str) -> dict:
    """AI inquiry analysis - parse customer inquiry into structured data.
    
    Extracts products, quantities, requirements, urgency, and generates
    a suggested reply draft. This replaces manual reading and note-taking.
    """
    if not settings.OPENAI_API_KEY:
        return _basic_inquiry_analysis(email_content)

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL)

        prompt = f"""You are an expert foreign trade inquiry analyst. Parse this customer inquiry email into structured data.

Customer Inquiry:
{email_content}

Return a JSON object:
{{
    "inquiry_type": "new_inquiry|repeat_order|price_request|sample_request|complaint|general",
    "customer_info": {{
        "name": "customer name if found",
        "company": "company name if found",
        "country": "country if mentioned",
        "contact_method": "how they found us"
    }},
    "products": [
        {{
            "name": "product name",
            "specification": "specs/model/size",
            "quantity": "quantity with unit",
            "target_price": "if mentioned",
            "requirements": "any special requirements"
        }}
    ],
    "delivery_requirements": {{
        "deadline": "when they need it",
        "shipping_terms": "FOB/CIF/EXW etc",
        "destination_port": "port if mentioned"
    }},
    "urgency": "high|medium|low",
    "urgency_reason": "why this urgency level",
    "customer_intent_score": 0-100,
    "intent_score_reason": "why this score",
    "key_concerns": ["what the customer cares about most"],
    "missing_info": ["what info we need to ask for"],
    "suggested_reply_draft": "a professional reply email in HTML addressing all their points and asking for missing info",
    "suggested_reply_subject": "subject line for the reply",
    "follow_up_strategy": "how to follow up on this inquiry"
}}

Return ONLY valid JSON."""

        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a B2B trade inquiry analyst. Always respond with valid JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=2000,
        )

        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        result = json.loads(content)
        result["ai_powered"] = True
        return result

    except Exception as e:
        logger.error(f"Inquiry analysis error: {e}")
        return _basic_inquiry_analysis(email_content)


def _basic_inquiry_analysis(email_content: str) -> dict:
    """Basic keyword-based inquiry analysis."""
    text_lower = email_content.lower()

    inquiry_type = "general"
    if any(w in text_lower for w in ["price", "quote", "quotation", "how much", "cost"]):
        inquiry_type = "price_request"
    elif any(w in text_lower for w in ["sample", "trial", "test"]):
        inquiry_type = "sample_request"
    elif any(w in text_lower for w in ["order", "purchase", "buy", "place"]):
        inquiry_type = "repeat_order"
    elif any(w in text_lower for w in ["interested", "inquiry", "looking for", "need"]):
        inquiry_type = "new_inquiry"

    urgency = "medium"
    if any(w in text_lower for w in ["urgent", "asap", "immediately", "rush"]):
        urgency = "high"
    elif any(w in text_lower for w in ["no rush", "whenever", "at your convenience"]):
        urgency = "low"

    # Extract basic shipping terms
    shipping = ""
    for term in ["fob", "cif", "exw", "ddp", "fca"]:
        if term in text_lower:
            shipping = term.upper()
            break

    return {
        "inquiry_type": inquiry_type,
        "customer_info": {
            "name": "Not detected",
            "company": "Not detected",
            "country": "Not detected",
            "contact_method": "Not detected",
        },
        "products": [],
        "delivery_requirements": {
            "deadline": "Not mentioned",
            "shipping_terms": shipping or "Not mentioned",
            "destination_port": "Not mentioned",
        },
        "urgency": urgency,
        "urgency_reason": "Basic keyword analysis",
        "customer_intent_score": 50,
        "intent_score_reason": "Add API key for deeper AI analysis",
        "key_concerns": [],
        "missing_info": ["Product specifications", "Quantity", "Delivery requirements"],
        "suggested_reply_draft": "<p>Dear Sir/Madam,</p><p>Thank you for your inquiry. We would be happy to assist you. Could you please provide more details about your requirements?</p><p>Best regards</p>",
        "suggested_reply_subject": "Re: Your Inquiry",
        "follow_up_strategy": "Reply promptly with product details and ask for missing specifications",
        "ai_powered": False,
    }


async def customer_churn_alerts(db_session=None) -> dict:
    """AI customer churn alert system - detect at-risk customers.
    
    Automatically scans ALL customers and identifies those at risk of churning:
    - No contact for extended periods
    - Stuck in stage too long
    - Emails sent but no replies
    - Quote sent but no follow-up
    
    This replaces the manual process of checking each customer one by one.
    """
    if not db_session:
        return {"error": "Database session required", "alerts": []}

    from sqlalchemy import select, func
    from app.models.customer import Customer, CustomerContact
    from app.models.email_log import EmailLog, EmailStatus
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    alerts = []

    # Get all active customers (not completed or lost)
    result = await db_session.execute(
        select(Customer).where(
            Customer.stage.notin_(["completed", "lost"])
        )
    )
    customers = result.scalars().all()

    for c in customers:
        risk_level = None
        risk_reasons = []
        suggested_actions = []

        # Get email stats for this customer
        email_result = await db_session.execute(
            select(EmailLog).where(EmailLog.customer_id == c.id).order_by(EmailLog.created_at.desc())
        )
        all_emails = email_result.scalars().all()
        last_email = all_emails[0] if all_emails else None
        total_sent = len([e for e in all_emails if e.status in ("SENT", "OPENED", "REPLIED")])
        total_replies = len([e for e in all_emails if e.status == "REPLIED"])

        # Risk Factor 1: No contact at all
        if c.stage == "new" and (now - c.created_at).days > 7:
            risk_level = "high"
            risk_reasons.append(f"New customer created {(now - c.created_at).days} days ago, never contacted")
            suggested_actions.append("Send initial outreach email immediately")

        # Risk Factor 2: No reply after multiple emails
        if total_sent >= 3 and total_replies == 0:
            risk_level = "critical"
            risk_reasons.append(f"Sent {total_sent} emails, received 0 replies")
            suggested_actions.append("Try a different channel (WhatsApp, phone) or change email approach")
        elif total_sent >= 2 and total_replies == 0:
            risk_level = "high"
            risk_reasons.append(f"Sent {total_sent} emails, no reply yet")
            suggested_actions.append("Send a follow-up with different angle or value proposition")

        # Risk Factor 3: Long time no contact
        last_contact = c.last_contacted_at or (last_email.created_at if last_email else None)
        if last_contact:
            days_since = (now - last_contact).days
            if c.stage in ("interested", "quoting") and days_since > 14:
                risk_level = "critical"
                risk_reasons.append(f"Hot lead gone cold - no contact for {days_since} days in {c.stage} stage")
                suggested_actions.append("Urgent: Call or WhatsApp immediately, then follow up with email")
            elif c.stage == "contacted" and days_since > 21:
                risk_level = "high"
                risk_reasons.append(f"No response for {days_since} days after initial contact")
                suggested_actions.append("Try re-engagement email with new product offer or discount")
            elif days_since > 30:
                risk_level = "medium"
                risk_reasons.append(f"No contact for {days_since} days - customer may have moved on")
                suggested_actions.append("Send a check-in email with industry news or new products")

        # Risk Factor 4: Stuck in stage
        if c.stage == "quoting" and (now - (c.updated_at or c.created_at)).days > 14:
            risk_level = "high"
            risk_reasons.append("Stuck in quoting stage for over 2 weeks")
            suggested_actions.append("Follow up on the quote - ask if they need samples or have concerns")
        elif c.stage == "sample" and (now - (c.updated_at or c.created_at)).days > 21:
            risk_level = "high"
            risk_reasons.append("Sample sent but no feedback for over 3 weeks")
            suggested_actions.append("Follow up on sample - ask about quality and next steps")

        # Risk Factor 5: Quoted price but went silent
        if c.stage == "quoting" and last_email and (now - last_email.created_at).days > 7:
            unread = last_email.status == "SENT"
            if unread:
                risk_reasons.append("Quote email sent but not opened")
                suggested_actions.append("Resend with different subject line or try WhatsApp")

        if risk_level:
            # Get primary contact
            contact_result = await db_session.execute(
                select(CustomerContact)
                .where(CustomerContact.customer_id == c.id, CustomerContact.is_primary == 1)
                .limit(1)
            )
            contact = contact_result.scalar_one_or_none()

            alerts.append({
                "customer_id": c.id,
                "company_name": c.company_name,
                "stage": c.stage,
                "country": c.country,
                "risk_level": risk_level,
                "risk_reasons": risk_reasons,
                "suggested_actions": suggested_actions,
                "contact_email": contact.email if contact else None,
                "contact_name": contact.name if contact else None,
                "last_contacted": last_contact.isoformat() if last_contact else None,
                "emails_sent": total_sent,
                "emails_replied": total_replies,
                "days_in_stage": (now - (c.updated_at or c.created_at)).days,
            })

    # Sort by risk level
    risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    alerts.sort(key=lambda x: risk_order.get(x["risk_level"], 4))

    # Generate AI rescue emails for critical/high risk customers with API key
    if settings.OPENAI_API_KEY and alerts:
        for alert in alerts[:5]:
            if alert["risk_level"] in ("critical", "high") and alert["contact_email"]:
                try:
                    from openai import AsyncOpenAI
                    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL)

                    prompt = f"""Write a rescue email for a customer at risk of being lost.

Company: {alert['company_name']}
Stage: {alert['stage']}
Risk reasons: {'; '.join(alert['risk_reasons'])}
Days since last contact: {(now - datetime.fromisoformat(alert['last_contacted'])).days if alert['last_contacted'] else 'Never'}

Write a warm, non-pushy email that re-engages the customer. Offer value (new products, special pricing, industry insights). Keep under 100 words. Return ONLY the email body in HTML."""

                    resp = await client.chat.completions.create(
                        model=settings.OPENAI_MODEL,
                        messages=[
                            {"role": "system", "content": "You are a professional foreign trade email writer. Write concise re-engagement emails."},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.7,
                        max_tokens=300,
                    )
                    alert["rescue_email"] = resp.choices[0].message.content.strip()
                    alert["rescue_subject"] = f"Something new for {alert['company_name']} - Let's reconnect"
                except Exception as e:
                    logger.error(f"Rescue email error: {e}")

    return {
        "total_alerts": len(alerts),
        "critical": len([a for a in alerts if a["risk_level"] == "critical"]),
        "high": len([a for a in alerts if a["risk_level"] == "high"]),
        "medium": len([a for a in alerts if a["risk_level"] == "medium"]),
        "alerts": alerts,
    }


# Holiday calendar for common international trade holidays
HOLIDAY_CALENDAR = [
    {"name": "Christmas", "date": "12-25", "greeting": "Merry Christmas!", "markets": ["US", "UK", "EU", "AU", "CA"]},
    {"name": "New Year", "date": "01-01", "greeting": "Happy New Year!", "markets": ["all"]},
    {"name": "Chinese New Year", "date": "02-01", "greeting": "Happy Chinese New Year! Our factory will resume on...", "markets": ["all"]},
    {"name": "Easter", "date": "04-01", "greeting": "Happy Easter!", "markets": ["US", "UK", "EU", "AU"]},
    {"name": "Thanksgiving", "date": "11-28", "greeting": "Happy Thanksgiving!", "markets": ["US", "CA"]},
    {"name": "Diwali", "date": "10-20", "greeting": "Happy Diwali!", "markets": ["IN"]},
    {"name": "Ramadan Eid", "date": "04-10", "greeting": "Eid Mubarak!", "markets": ["AE", "SA", "EG", "TR"]},
    {"name": "Eid al-Adha", "date": "06-17", "greeting": "Eid al-Adha Mubarak!", "markets": ["AE", "SA", "EG", "TR"]},
    {"name": "National Day", "date": "10-01", "greeting": "Happy National Day! We will be on holiday from Oct 1-7.", "markets": ["all"]},
]


async def generate_holiday_emails(
    holiday_name: str,
    company_name: str = "",
    custom_message: str = "",
    db_session=None,
) -> dict:
    """AI generates personalized holiday greeting emails for all customers.
    
    Instead of manually sending holiday greetings one by one:
    1. Select a holiday
    2. AI scans all customers
    3. Generates personalized greeting for each
    4. One-click send all
    """
    if not db_session:
        return {"error": "Database session required", "emails": []}

    from sqlalchemy import select
    from app.models.customer import Customer, CustomerContact

    # Find the holiday info
    holiday_info = None
    for h in HOLIDAY_CALENDAR:
        if h["name"].lower() == holiday_name.lower():
            holiday_info = h
            break

    if not holiday_info:
        holiday_info = {"name": holiday_name, "greeting": f"Happy {holiday_name}!", "markets": ["all"]}

    # Get all customers with contacts
    result = await db_session.execute(select(Customer).where(Customer.stage.notin_(["lost"])))
    customers = result.scalars().all()

    if not customers:
        return {"total": 0, "generated": 0, "emails": [], "message": "No customers found"}

    emails = []

    for c in customers:
        # Get primary contact
        contact_result = await db_session.execute(
            select(CustomerContact)
            .where(CustomerContact.customer_id == c.id, CustomerContact.is_primary == 1)
            .limit(1)
        )
        contact = contact_result.scalar_one_or_none()
        if not contact or not contact.email:
            continue

        # AI or template-based email generation
        if settings.OPENAI_API_KEY:
            try:
                from openai import AsyncOpenAI
                client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL)

                prompt = f"""Write a warm holiday greeting email for a business contact.

Holiday: {holiday_info['name']}
Greeting: {holiday_info['greeting']}
Your company: {company_name or 'Our company'}
Customer company: {c.company_name}
Customer country: {c.country or 'Not specified'}
Customer industry: {c.industry or 'Not specified'}
Relationship stage: {c.stage}
{f'Custom message: {custom_message}' if custom_message else ''}

Rules:
- Keep it warm but professional (B2B tone)
- Reference their industry/country if possible
- Include the holiday greeting naturally
- If it's Chinese New Year, mention factory holiday dates
- Keep under 80 words
- Do NOT push products aggressively

Return JSON: {{"subject": "email subject", "body": "email body in HTML"}}"""

                resp = await client.chat.completions.create(
                    model=settings.OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": "Write professional B2B holiday greeting emails. Return valid JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.7,
                    max_tokens=400,
                )

                content = resp.choices[0].message.content.strip()
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                result_data = json.loads(content)

                emails.append({
                    "customer_id": c.id,
                    "company_name": c.company_name,
                    "contact_name": contact.name,
                    "to_email": contact.email,
                    "subject": result_data.get("subject", f"{holiday_info['greeting']} - {company_name or 'Warm wishes'}"),
                    "body": result_data.get("body", ""),
                    "personalized": True,
                })
                continue
            except Exception as e:
                logger.error(f"Holiday email AI error for {c.company_name}: {e}")

        # Fallback: template-based
        subject = f"{holiday_info['greeting']} - {company_name or 'Best Wishes'}"
        body = f"""<p>Dear {contact.name},</p>
<p>{holiday_info['greeting']}</p>
<p>On this special occasion, we at <strong>{company_name or 'our company'}</strong> would like to express our sincere gratitude for your partnership and trust.</p>
{f'<p>{custom_message}</p>' if custom_message else ''}
<p>We look forward to continuing our cooperation in the coming year.</p>
<p>Warm regards,<br/>{company_name or 'Our Team'}</p>"""

        emails.append({
            "customer_id": c.id,
            "company_name": c.company_name,
            "contact_name": contact.name,
            "to_email": contact.email,
            "subject": subject,
            "body": body,
            "personalized": False,
        })

    return {
        "holiday": holiday_info["name"],
        "greeting": holiday_info["greeting"],
        "total_customers": len(customers),
        "generated": len(emails),
        "emails": emails,
    }


def get_holiday_calendar() -> list:
    """Return the holiday calendar with upcoming holidays highlighted."""
    from datetime import datetime
    now = datetime.utcnow()
    current_month_day = now.strftime("%m-%d")

    result = []
    for h in HOLIDAY_CALENDAR:
        holiday_date = h["date"]
        # Calculate days until
        holiday_month = int(holiday_date.split("-")[0])
        holiday_day = int(holiday_date.split("-")[1])
        current_month = now.month
        current_day = now.day

        if holiday_month > current_month or (holiday_month == current_month and holiday_day >= current_day):
            days_until = (datetime(now.year, holiday_month, holiday_day) - now).days
        else:
            days_until = (datetime(now.year + 1, holiday_month, holiday_day) - now).days

        result.append({
            **h,
            "days_until": days_until,
            "upcoming": days_until <= 30,
        })

    result.sort(key=lambda x: x["days_until"])
    return result


async def auto_lead_scanner(
    product_keywords: str,
    target_country: str = "",
    target_region: str = "",
    max_results: int = 10,
) -> dict:
    """Auto Lead Scanner - automatically find potential buyers from Google.
    
    Instead of manually Googling "gold thread buyer Germany" and visiting each site:
    1. Search Google with product + country keywords
    2. Visit each result page
    3. Extract company info (name, email, phone, products)
    4. Score relevance
    5. Return structured lead list ready to import into CRM
    
    This is the CORE automation that saves hours of manual prospecting.
    """
    import requests
    from bs4 import BeautifulSoup

    leads = []

    # Build search queries
    queries = [
        f"{product_keywords} buyer {target_country}",
        f"{product_keywords} importer {target_country}",
        f"{product_keywords} wholesale {target_country}",
        f'"{product_keywords}" company {target_country} email',
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    seen_domains = set()
    all_urls = []

    # Step 1: Search using ddgs library
    try:
        from ddgs import DDGS
        ddgs = DDGS()
        for query in queries[:2]:
            try:
                results = list(ddgs.text(query, max_results=max_results))
                for r in results:
                    href = r.get("href", "")
                    if href and href.startswith("http"):
                        domain = href.split("/")[2] if len(href.split("/")) > 2 else href
                        if domain not in seen_domains:
                            seen_domains.add(domain)
                            all_urls.append(href)
                            logger.info(f"Found: {domain} - {r.get('title', '')[:50]}")
            except Exception as e:
                logger.error(f"DDGS search error for '{query}': {e}")
    except ImportError:
        logger.warning("ddgs not installed, falling back to basic scraping")
    except Exception as e:
        logger.error(f"DDGS error: {e}")

    logger.info(f"Lead Scanner found {len(all_urls)} URLs from search")

    # Step 2: Visit each URL and extract company info
    for url in all_urls[:max_results]:
        try:
            resp = requests.get(url, headers=headers, timeout=10, verify=False)
            soup = BeautifulSoup(resp.text, "lxml")

            # Remove noise
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()

            page_text = soup.get_text(separator="\n", strip=True)[:5000]
            title = soup.title.string if soup.title else ""
            text_lower = page_text.lower()

            # Extract emails from page
            email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
            emails = list(set(re.findall(email_pattern, page_text)))
            # Filter out common non-company emails
            emails = [e for e in emails if not any(x in e.lower() for x in
                ["noreply", "no-reply", "support@", "info@google", "example.com",
                 "sentry.io", "wixpress", "wordpress", "@s.", ".png", ".jpg"])]

            # Extract phone numbers
            phone_pattern = r'[\+]?[\d\s\-\(\)]{7,15}'
            phones = re.findall(phone_pattern, page_text)
            phones = [p.strip() for p in phones if len(p.strip()) >= 8][:3]

            # Detect company name from title
            company_name = title.split("-")[0].strip() if title else ""
            company_name = company_name.split("|")[0].strip()

            # Detect country clues
            detected_country = target_country or "Unknown"
            country_keywords = {
                "Germany": [".de", "gmbh", "german"],
                "United States": [".com", "llc", "inc", "corp"],
                "United Kingdom": [".co.uk", "ltd", "limited"],
                "France": [".fr", "sarl", "sas"],
                "Italy": [".it", "srl", "spa"],
                "Spain": [".es", "sl", "sa"],
                "Netherlands": [".nl", "bv"],
                "India": [".in", "pvt"],
                "Brazil": [".br", "ltda"],
                "Australia": [".au", "pty"],
            }
            url_lower = url.lower()
            if not target_country:
                for country, clues in country_keywords.items():
                    if any(c in url_lower for c in clues) or any(c in text_lower for c in clues):
                        detected_country = country
                        break

            # Check if page is relevant (buyer/importer)
            relevance_score = 20  # base
            buyer_keywords = ["import", "buyer", "wholesale", "distributor", "retailer",
                            "sourcing", "procurement", "supply chain", "trade",
                            "importer", "importers", "buyer", "buyers", "purchasing"]
            b2b_platforms = ["exporthub.com", "tradekey.com", "alibaba.com", "made-in-china.com",
                           "globalsources.com", "indiamart.com", "volza.com", "importgenius.com",
                           "panjiva.com", "tradedata.net", "kompass.com", "thomasnet.com"]
            
            # B2B platform boost
            is_b2b = any(p in url_lower for p in b2b_platforms)
            if is_b2b:
                relevance_score += 30
            
            if any(kw in text_lower for kw in buyer_keywords):
                relevance_score += 20
            # Check for product keywords (fuzzy match - partial)
            product_parts = product_keywords.lower().split()
            if any(part in text_lower for part in product_parts if len(part) > 3):
                relevance_score += 20
            if product_keywords.lower() in text_lower:
                relevance_score += 15
            if emails:
                relevance_score += 10
            if "contact" in text_lower or "inquiry" in text_lower:
                relevance_score += 5

            # Only include if somewhat relevant
            if relevance_score >= 30:
                leads.append({
                    "company_name": company_name or "Unknown",
                    "website": url,
                    "country": detected_country,
                    "emails": emails[:3],
                    "phones": phones[:2],
                    "relevance_score": min(relevance_score, 100),
                    "page_title": title[:200],
                    "snippet": page_text[:300].replace("\n", " "),
                    "has_contact": bool(emails),
                })

        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")

    # Sort by relevance
    leads.sort(key=lambda x: x["relevance_score"], reverse=True)

    # AI enhancement: if API key available, use AI to score and enrich top leads
    if settings.OPENAI_API_KEY and leads:
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL)

            leads_summary = "\n".join([
                f"- {l['company_name']} ({l['website']}) - {l['country']} - Score: {l['relevance_score']}"
                for l in leads[:8]
            ])

            prompt = f"""You are a foreign trade lead analyst. Review these leads found by Google search for "{product_keywords}" in {target_country}.

Leads:
{leads_summary}

For each lead, provide a brief assessment. Return JSON:
{{
    "recommendations": [
        {{
            "company_name": "name",
            "priority": "high|medium|low",
            "reason": "why this is a good/bad lead",
            "approach_angle": "how to approach this customer",
            "estimated_potential": "small/medium/large order"
        }}
    ],
    "market_insights": "brief insights about this market for these products",
    "search_tips": "suggestions for better search queries"
}}

Return ONLY valid JSON."""

            resp = await client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You are a B2B trade lead analyst. Return valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=1500,
            )

            content = resp.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            ai_analysis = json.loads(content)

            # Merge AI analysis into leads
            for rec in ai_analysis.get("recommendations", []):
                for lead in leads:
                    if lead["company_name"] == rec.get("company_name"):
                        lead["ai_priority"] = rec.get("priority", "medium")
                        lead["ai_reason"] = rec.get("reason", "")
                        lead["approach_angle"] = rec.get("approach_angle", "")
                        lead["estimated_potential"] = rec.get("estimated_potential", "unknown")
                        break

            return {
                "total_found": len(leads),
                "leads": leads,
                "market_insights": ai_analysis.get("market_insights", ""),
                "search_tips": ai_analysis.get("search_tips", ""),
                "ai_powered": True,
                "search_queries_used": queries[:2],
            }

        except Exception as e:
            logger.error(f"AI enhancement error: {e}")

    return {
        "total_found": len(leads),
        "leads": leads,
        "market_insights": "",
        "search_tips": "Try different keyword combinations for better results",
        "ai_powered": False,
        "search_queries_used": queries[:2],
    }


async def generate_pi(
    customer_name: str,
    customer_company: str,
    customer_address: str,
    customer_email: str,
    products: list,
    trade_terms: str = "FOB",
    payment_terms: str = "T/T 30% deposit, 70% before shipment",
    validity_days: int = 15,
    your_company: str = "",
    your_address: str = "",
    notes: str = "",
) -> dict:
    """Generate a professional Proforma Invoice (PI).
    
    Instead of manually creating PI in Word/Excel:
    1. Input customer info and product list
    2. AI generates a professional PI with proper formatting
    3. Auto-calculates totals
    4. Returns HTML that can be printed as PDF or emailed
    
    products format: [{"name": "...", "spec": "...", "qty": 100, "unit": "pcs", "unit_price": 5.50}]
    """
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    pi_number = f"PI-{now.strftime('%Y%m%d')}-{str(hash(customer_company))[-4:]}"
    pi_date = now.strftime("%Y-%m-%d")
    valid_until = (now + timedelta(days=validity_days)).strftime("%Y-%m-%d")

    # Calculate totals
    items = []
    subtotal = 0
    for i, p in enumerate(products, 1):
        qty = float(p.get("qty", 0))
        price = float(p.get("unit_price", 0))
        amount = qty * price
        subtotal += amount
        items.append({
            "no": i,
            "name": p.get("name", ""),
            "spec": p.get("spec", p.get("specification", "")),
            "qty": qty,
            "unit": p.get("unit", "pcs"),
            "unit_price": price,
            "amount": round(amount, 2),
        })

    # Build professional PI HTML
    pi_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; color: #333; font-size: 13px; }}
.pi-container {{ max-width: 800px; margin: 0 auto; }}
.header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 30px; border-bottom: 3px solid #2c3e50; padding-bottom: 15px; }}
.company-info h1 {{ margin: 0; color: #2c3e50; font-size: 24px; }}
.company-info p {{ margin: 2px 0; color: #666; }}
.pi-title {{ text-align: right; }}
.pi-title h2 {{ margin: 0; color: #e74c3c; font-size: 28px; letter-spacing: 2px; }}
.pi-title p {{ margin: 4px 0; color: #666; }}
.parties {{ display: flex; justify-content: space-between; margin-bottom: 25px; }}
.buyer, .seller {{ width: 48%; }}
.buyer h3, .seller h3 {{ margin: 0 0 8px; color: #2c3e50; font-size: 14px; border-bottom: 1px solid #eee; padding-bottom: 4px; }}
.buyer p, .seller p {{ margin: 2px 0; color: #555; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
th {{ background: #2c3e50; color: white; padding: 10px 8px; text-align: left; font-size: 12px; }}
td {{ padding: 8px; border-bottom: 1px solid #eee; }}
tr:nth-child(even) {{ background: #f9f9f9; }}
.amount-col {{ text-align: right; }}
.totals {{ float: right; width: 300px; }}
.totals table {{ margin: 0; }}
.totals td {{ padding: 6px 8px; }}
.totals .grand-total {{ font-weight: bold; font-size: 16px; background: #2c3e50; color: white; }}
.terms {{ clear: both; margin-top: 20px; padding: 15px; background: #f5f5f5; border-radius: 4px; }}
.terms h3 {{ margin: 0 0 10px; color: #2c3e50; }}
.terms p {{ margin: 4px 0; color: #555; }}
.footer {{ margin-top: 30px; text-align: center; color: #999; font-size: 11px; border-top: 1px solid #eee; padding-top: 10px; }}
.stamp {{ margin-top: 40px; display: flex; justify-content: space-between; }}
.stamp-box {{ width: 200px; border-top: 1px solid #333; padding-top: 5px; text-align: center; color: #666; }}
</style>
</head>
<body>
<div class="pi-container">
  <div class="header">
    <div class="company-info">
      <h1>{your_company or 'Your Company Name'}</h1>
      <p>{your_address or 'Your Address, Ningbo, China'}</p>
      <p>Email: sales@yourcompany.com | Tel: +86-xxx-xxxx-xxxx</p>
    </div>
    <div class="pi-title">
      <h2>PROFORMA INVOICE</h2>
      <p><strong>PI No:</strong> {pi_number}</p>
      <p><strong>Date:</strong> {pi_date}</p>
      <p><strong>Valid Until:</strong> {valid_until}</p>
    </div>
  </div>

  <div class="parties">
    <div class="buyer">
      <h3>BUYER</h3>
      <p><strong>{customer_company}</strong></p>
      <p>Attn: {customer_name}</p>
      <p>{customer_address}</p>
      <p>Email: {customer_email}</p>
    </div>
    <div class="seller">
      <h3>TERMS</h3>
      <p><strong>Trade Terms:</strong> {trade_terms}</p>
      <p><strong>Payment:</strong> {payment_terms}</p>
      <p><strong>Delivery:</strong> 15-25 days after deposit</p>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th>No.</th>
        <th>Product</th>
        <th>Specification</th>
        <th>Quantity</th>
        <th>Unit</th>
        <th class="amount-col">Unit Price (USD)</th>
        <th class="amount-col">Amount (USD)</th>
      </tr>
    </thead>
    <tbody>"""

    for item in items:
        pi_html += f"""
      <tr>
        <td>{item['no']}</td>
        <td>{item['name']}</td>
        <td>{item['spec']}</td>
        <td class="amount-col">{item['qty']:,.0f}</td>
        <td>{item['unit']}</td>
        <td class="amount-col">${item['unit_price']:,.2f}</td>
        <td class="amount-col">${item['amount']:,.2f}</td>
      </tr>"""

    pi_html += f"""
    </tbody>
  </table>

  <div class="totals">
    <table>
      <tr><td>Subtotal:</td><td class="amount-col">${subtotal:,.2f}</td></tr>
      <tr class="grand-total"><td>TOTAL:</td><td class="amount-col">${subtotal:,.2f}</td></tr>
    </table>
  </div>

  <div class="terms">
    <h3>TERMS AND CONDITIONS</h3>
    <p>1. Payment: {payment_terms}</p>
    <p>2. Delivery: 15-25 working days after receiving deposit</p>
    <p>3. This PI is valid until {valid_until}</p>
    <p>4. Prices are in USD, {trade_terms} basis</p>
    {f'<p>5. Notes: {notes}</p>' if notes else ''}
  </div>

  <div class="stamp">
    <div class="stamp-box">Seller's Signature & Stamp</div>
    <div class="stamp-box">Buyer's Confirmation</div>
  </div>

  <div class="footer">
    <p>This is a Proforma Invoice, not a tax invoice. For business inquiries only.</p>
  </div>
</div>
</body>
</html>"""

    return {
        "pi_number": pi_number,
        "pi_date": pi_date,
        "valid_until": valid_until,
        "customer_name": customer_name,
        "customer_company": customer_company,
        "items": items,
        "subtotal": subtotal,
        "total": subtotal,
        "trade_terms": trade_terms,
        "payment_terms": payment_terms,
        "pi_html": pi_html,
    }
