"""Automated workflows - the 'brain' that connects all tools.

Instead of you operating each tool, workflows run them automatically
and compile results into a human-readable report.
"""
import logging
import asyncio
from datetime import datetime

from app.notification import send_notification

logger = logging.getLogger(__name__)


async def scan_leads_draft_workflow(
    product_keywords: str = "embroidery thread, gold metallic yarn",
    target_country: str = "",
    max_results: int = 10,
) -> dict:
    """Step 1: Search for new customers and generate draft emails (no sending).

    Returns a list of leads with AI-generated email drafts for human review.
    """
    from app.services.agent_service import auto_lead_scanner
    from app.config import settings

    result_summary = {
        "scan_time": datetime.now().isoformat(),
        "keywords": product_keywords,
        "country": target_country,
        "total_found": 0,
        "with_email": 0,
        "leads": [],
    }

    # Step 1: Scan for leads
    logger.info(f"Step 1: Scanning for leads with keywords '{product_keywords}' in '{target_country}'...")
    scan_result = await auto_lead_scanner(
        product_keywords=product_keywords,
        target_country=target_country,
        max_results=max_results,
    )

    leads = scan_result.get("leads", [])
    result_summary["total_found"] = len(leads)
    logger.info(f"Found {len(leads)} leads")

    # Filter leads with emails
    leads_with_email = [l for l in leads if l.get("emails")]
    result_summary["with_email"] = len(leads_with_email)
    logger.info(f"{len(leads_with_email)} leads have email addresses")

    # Prepare leads with draft emails for review
    reviewed_leads = []
    async with _get_db_session() as db:
        from app.models.customer import Customer
        from sqlalchemy import select

        for lead in leads_with_email:
            to_email = lead["emails"][0]
            company_name = lead.get("company_name", "Unknown")
            country = lead.get("country", "")
            website = lead.get("website", "")

            # Check if customer already exists
            exists = False
            if website:
                existing = await db.execute(
                    select(Customer).where(Customer.website == website).limit(1)
                )
                if existing.scalar_one_or_none():
                    exists = True

            reviewed_leads.append({
                "company_name": company_name,
                "country": country,
                "website": website,
                "email": to_email,
                "relevance_score": lead.get("relevance_score", 0),
                "snippet": lead.get("snippet", "")[:200],
                "draft_subject": f"Partnership Inquiry - {product_keywords.split(',')[0].strip()} from China",
                "draft_body": lead.get("draft_email", "") or f"Dear {company_name},\n\nI noticed your company in {country} and thought our products might be a good fit.\n\nBest regards",
                "already_exists": exists,
            })

    result_summary["leads"] = reviewed_leads
    return result_summary


async def send_drafted_emails_workflow(
    selected_leads: list[dict],
    product_keywords: str = "embroidery thread, gold metallic yarn",
) -> dict:
    """Step 2: Send selected draft emails and save to CRM.

    selected_leads: list of dicts with company_name, country, website,
                    email, draft_subject, draft_body
    """
    from app.config import settings

    result_summary = {
        "total_selected": len(selected_leads),
        "emails_sent": 0,
        "emails_failed": 0,
        "crm_saved": 0,
        "send_results": [],
    }

    if not selected_leads:
        result_summary["message"] = "No leads selected"
        return result_summary

    async with _get_db_session() as db:
        from app.models.customer import Customer, CustomerContact, CustomerSource, CustomerStage
        from app.models.email_log import EmailLog, EmailStatus
        from sqlalchemy import select

        for lead in selected_leads:
            to_email = lead.get("email", "")
            company_name = lead.get("company_name", "Unknown")
            country = lead.get("country", "")
            website = lead.get("website", "")
            subject = lead.get("draft_subject", "")
            body = lead.get("draft_body", "")

            if not to_email:
                result_summary["send_results"].append({"company": company_name, "status": "skipped", "reason": "No email"})
                continue

            send_result = {"company": company_name, "email": to_email}

            # Send email via SMTP
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                try:
                    from app.utils.email_sender import send_email as smtp_send
                    result = await smtp_send(
                        to_email=to_email,
                        to_name=company_name,
                        subject=subject,
                        body=body,
                    )
                    if result.get("success"):
                        result_summary["emails_sent"] += 1
                        send_result["status"] = "sent"
                    else:
                        result_summary["emails_failed"] += 1
                        send_result["status"] = "failed"
                        send_result["error"] = result.get("error", "Unknown error")
                except Exception as e:
                    result_summary["emails_failed"] += 1
                    send_result["status"] = "failed"
                    send_result["error"] = str(e)
            else:
                result_summary["emails_sent"] += 1
                send_result["status"] = "simulated"

            # Save to CRM
            try:
                if website:
                    existing_customer = await db.execute(
                        select(Customer).where(Customer.website == website).limit(1)
                    )
                    if existing_customer.scalar_one_or_none():
                        send_result["crm"] = "already_exists"
                        result_summary["send_results"].append(send_result)
                        continue

                new_customer = Customer(
                    company_name=company_name,
                    country=country,
                    website=website,
                    industry="Textile",
                    products=product_keywords,
                    source=CustomerSource.SCRAPER,
                    stage=CustomerStage.CONTACTED,
                    score=float(lead.get("relevance_score", 50)),
                    tags="auto-scan-send",
                    notes=lead.get("snippet", "")[:500],
                )
                db.add(new_customer)
                await db.flush()

                contact = CustomerContact(
                    customer_id=new_customer.id,
                    name=company_name[:100],
                    email=to_email,
                    is_primary=1,
                )
                db.add(contact)

                email_log = EmailLog(
                    customer_id=new_customer.id,
                    to_email=to_email,
                    to_name=company_name[:200],
                    subject=subject,
                    body=body,
                    status=EmailStatus.SENT if send_result["status"] in ("sent", "simulated") else EmailStatus.FAILED,
                )
                db.add(email_log)

                result_summary["crm_saved"] += 1
                send_result["crm"] = "saved"
                send_result["customer_id"] = new_customer.id

            except Exception as e:
                logger.error(f"CRM save error for {company_name}: {e}")
                send_result["crm"] = "error"
                send_result["crm_error"] = str(e)

            result_summary["send_results"].append(send_result)

        try:
            await db.commit()
        except Exception as e:
            logger.error(f"DB commit error: {e}")
            await db.rollback()

    send_notification(
        title=f"Scan & Send: {result_summary['emails_sent']} emails sent",
        body=f"Sent {result_summary['emails_sent']} emails, saved {result_summary['crm_saved']} to CRM.",
        level="info" if result_summary["emails_failed"] == 0 else "warning",
    )

    return result_summary


def _get_db_session():
    """Get a database session as an async context manager."""
    from app.database import async_session

    class _SessionCtx:
        def __init__(self):
            self._session = None

        async def __aenter__(self):
            self._session = async_session()
            return self._session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if self._session:
                await self._session.close()

    return _SessionCtx()


async def daily_morning_routine(search_keywords: str = None, target_country: str = "") -> dict:
    """Daily morning routine - the main automation workflow.

    Steps:
    1. Scan for new leads
    2. Check customer churn alerts
    3. Compile a morning report
    4. Send notification
    """
    report = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "new_leads": [],
        "churn_alerts": [],
        "follow_ups": [],
    }

    # Use config defaults if not specified
    if not search_keywords:
        from app.config import settings
        search_keywords = settings.DEFAULT_PRODUCT_KEYWORDS.split(",")[0].strip()

    # Step 1: Scan for new leads
    logger.info(f"Step 1: Scanning for new leads: '{search_keywords}'...")
    try:
        from app.services.agent_service import auto_lead_scanner
        scan_result = await auto_lead_scanner(
            product_keywords=search_keywords,
            target_country=target_country,
            max_results=5,
        )
        report["new_leads"] = scan_result.get("leads", [])
        report["leads_count"] = len(report["new_leads"])
    except Exception as e:
        logger.error(f"Lead scan failed: {e}")
        report["leads_count"] = 0

    # Step 1.5: Auto-generate draft emails for new leads ("Write" capability)
    logger.info("Step 1.5: Generating draft emails for new leads...")
    report["draft_emails"] = []
    for lead in report.get("new_leads", [])[:5]:
        draft = await _generate_lead_draft_email(lead, search_keywords)
        if draft:
            report["draft_emails"].append(draft)

    # Step 2: Check churn alerts (needs db session)
    logger.info("Step 2: Checking customer churn alerts...")
    try:
        from app.database import async_session
        from app.services.agent_service import customer_churn_alerts
        async with async_session() as db:
            churn_result = await customer_churn_alerts(db_session=db)
            report["churn_alerts"] = churn_result.get("alerts", [])
            report["critical_count"] = churn_result.get("critical", 0)
            report["high_count"] = churn_result.get("high", 0)
    except Exception as e:
        logger.error(f"Churn check failed: {e}")
        report["critical_count"] = 0
        report["high_count"] = 0

    # Step 3: Check follow-ups needed
    logger.info("Step 3: Checking follow-ups...")
    try:
        from app.database import async_session
        from app.services.agent_service import daily_follow_up_intelligence
        async with async_session() as db:
            follow_result = await daily_follow_up_intelligence(db_session=db)
            report["follow_ups"] = follow_result.get("actions", [])
            report["high_priority_followups"] = follow_result.get("high_priority", 0)
    except Exception as e:
        logger.error(f"Follow-up check failed: {e}")
        report["high_priority_followups"] = 0

    # Step 4: Compile and send morning report
    morning_report = _compile_morning_report(report)
    send_notification(
        title="TradeAgent Daily Morning Report",
        body=morning_report,
        level="info" if report.get("critical_count", 0) == 0 else "warning",
    )

    return report


def _compile_morning_report(report: dict) -> str:
    """Compile raw data into a readable morning report."""
    lines = []
    lines.append(f"Good morning! Here is your TradeAgent report for {report['date']}:")
    lines.append("")

    # New leads section
    leads_count = report.get("leads_count", 0)
    lines.append(f"[New Leads] Found {leads_count} potential customers:")
    for lead in report.get("new_leads", [])[:5]:
        name = lead.get("company_name", "Unknown")
        country = lead.get("country", "?")
        score = lead.get("relevance_score", 0)
        emails = lead.get("emails", [])
        email_str = f" | Email: {emails[0]}" if emails else ""
        lines.append(f"  - {name} ({country}) Score: {score}{email_str}")
    if leads_count == 0:
        lines.append("  No new leads found today.")
    lines.append("")

    # Churn alerts section
    critical = report.get("critical_count", 0)
    high = report.get("high_count", 0)
    if critical > 0 or high > 0:
        lines.append(f"[Alerts] {critical} critical, {high} high-risk customers need attention:")
        for alert in report.get("churn_alerts", [])[:3]:
            name = alert.get("company_name", "?")
            level = alert.get("risk_level", "?")
            reasons = alert.get("risk_reasons", [])
            reason = reasons[0] if reasons else "Unknown risk"
            lines.append(f"  - [{level.upper()}] {name}: {reason}")
        lines.append("")
    else:
        lines.append("[Alerts] No customer churn alerts.")
        lines.append("")

    # Follow-ups section
    followups = report.get("high_priority_followups", 0)
    if followups > 0:
        lines.append(f"[Follow-ups] {followups} customers need follow-up today:")
        for fu in report.get("follow_ups", [])[:3]:
            name = fu.get("company_name", "?")
            action = fu.get("suggested_action", "")
            lines.append(f"  - {name}: {action}")
        lines.append("")
    else:
        lines.append("[Follow-ups] No urgent follow-ups today.")
        lines.append("")

    # Summary
    total_actions = (leads_count > 0) + (critical + high > 0) + (followups > 0)
    if total_actions > 0:
        lines.append(f"Action needed: {total_actions} items require your attention.")
    else:
        lines.append("All clear! No urgent actions needed today.")

    return "\n".join(lines)


async def _generate_lead_draft_email(lead: dict, product_keywords: str) -> dict:
    """Generate a personalized draft email for a lead using AI.
    
    This is the "Write" capability - instead of templates,
    each email is crafted based on the lead's industry, country,
    and what they do.
    """
    company_name = lead.get("company_name", "Unknown")
    country = lead.get("country", "Unknown")
    website = lead.get("website", "")
    snippet = lead.get("snippet", "")
    emails = lead.get("emails", [])

    if not emails:
        return None

    try:
        from app.config import settings
        if settings.OPENAI_API_KEY:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL)

            prompt = f"""Write a personalized cold outreach email (120-180 words) for a foreign trade business.

Target company: {company_name}
Country: {country}
Website: {website}
Their business: {snippet[:300] if snippet else 'Unknown'}

Your product: {product_keywords} (from Ningbo, China)

Requirements:
- Reference their specific business, products, or industry (not generic)
- Explain WHY your product is relevant to THEIR specific business
- Mention product advantages: quality consistency, competitive MOQ, fast delivery
- Offer a concrete next step: free samples, catalog with color card, or trial order
- Include flexibility on customization (colors, specs, packaging)
- Keep 120-180 words, warm and professional
- Don't sound like a template

Return JSON: {{"subject": "email subject", "body": "email body in plain text"}}"""

            resp = await client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You write personalized B2B cold emails for a Chinese textile supplier. Each email must be 120-180 words, reference specific details, include product benefits, and a concrete next step. Return valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                max_tokens=500,
            )

            import json
            content = resp.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            result = json.loads(content)

            return {
                "company_name": company_name,
                "to_email": emails[0],
                "subject": result.get("subject", f"Business inquiry - {product_keywords}"),
                "body": result.get("body", ""),
                "body_preview": result.get("body", "")[:100] + "...",
            }
    except Exception as e:
        logger.error(f"Draft email generation failed for {company_name}: {e}")

    # Fallback: simple template
    return {
        "company_name": company_name,
        "to_email": emails[0],
        "subject": f"Quality {product_keywords} supplier - let's connect",
        "body": f"Dear {company_name},\n\nI noticed your company in {country} and thought our {product_keywords} might be a good fit for your business.\n\nWould you be open to a quick chat about how we can work together?\n\nBest regards",
        "body_preview": f"Dear {company_name}, I noticed your company in {country}...",
    }
