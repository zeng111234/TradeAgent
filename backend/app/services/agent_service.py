"""AI Agent service - deep AI integration for foreign trade."""
import logging
import json
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