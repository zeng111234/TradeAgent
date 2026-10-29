"""Automated workflows - the 'brain' that connects all tools.

Instead of you operating each tool, workflows run them automatically
and compile results into a human-readable report.
"""
import logging
import asyncio
from datetime import datetime

from app.llm import LLMTruncated, WRITING_MAX_TOKENS, complete_json, is_configured
from app.notification import send_notification

logger = logging.getLogger(__name__)


async def scan_leads_draft_workflow(
    product_keywords: str = "embroidery thread, gold metallic yarn",
    target_country: str = "",
    max_results: int = 10,
) -> dict:
    """Step 1: Search for new customers and generate draft emails (no sending).

    Returns a list of leads with email drafts for human review. Each draft says
    whether the model wrote it (``ai_powered``) or whether it is the built-in
    template (``fallback_reason`` explains why).
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

            # Actually write the draft. This used to read `lead["draft_email"]`,
            # a key the scanner has never set, so every "AI-generated draft"
            # here was a fixed one-line template and every subject was the same
            # string -- while the endpoint docstring promised AI drafts, and
            # the UI offered them as something to approve and send.
            draft = await _generate_lead_draft_email(lead, product_keywords)

            reviewed_leads.append({
                "company_name": company_name,
                "country": country,
                "website": website,
                "email": to_email,
                "relevance_score": lead.get("relevance_score", 0),
                "snippet": lead.get("snippet", "")[:200],
                "draft_subject": draft["subject"],
                "draft_body": draft["body"],
                "ai_powered": draft.get("ai_powered", False),
                "fallback_reason": draft.get("fallback_reason"),
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
        from app.services.email_claims import release
        from app.services.smtp_accounts import resolve_active_sender
        from sqlalchemy import select

        # Resolved once, before the first message: a switch landing halfway
        # through a run would split it across two mailboxes.
        workflow_sender = await resolve_active_sender(db)

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

            # Record first, then send, then settle -- the same three-step shape
            # as ``app/services/email_claims.py``, and for the same reason. This
            # loop used to send every message and write all the records in one
            # commit at the end, so a crash mid-batch left every mail that had
            # already gone out with no row at all -- and the next run, finding
            # no customer and no log, sent them again. The claim is what makes
            # a crash cost at most the one in-flight attempt.
            try:
                customer_id = None
                if website:
                    existing_customer = await db.execute(
                        select(Customer).where(Customer.website == website).limit(1)
                    )
                    existing = existing_customer.scalar_one_or_none()
                    if existing is not None:
                        # Still record the send against the customer that is
                        # already there. A mail to a known customer with no log
                        # row is an invisible send: invisible to the trace, to
                        # reply tracking, and to anyone asking "did we already
                        # write to these people?"
                        customer_id = existing.id
                        send_result["crm"] = "already_exists"

                if customer_id is None:
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
                    customer_id = new_customer.id
                    result_summary["crm_saved"] += 1
                    send_result["crm"] = "saved"
                    send_result["customer_id"] = customer_id
                else:
                    send_result["customer_id"] = customer_id

                # The claim: a row that says an attempt is in flight, committed
                # *before* anything leaves the process. If this cannot be
                # written, the send does not happen -- an unrecorded send is
                # exactly what the claim exists to prevent.
                email_log = EmailLog(
                    customer_id=customer_id,
                    to_email=to_email,
                    to_name=company_name[:200],
                    subject=subject,
                    body=body,
                    status=EmailStatus.SENDING,
                )
                db.add(email_log)
                await db.commit()

            except Exception as e:
                await db.rollback()
                logger.error(f"CRM claim error for {company_name}: {e}")
                result_summary["emails_failed"] += 1
                send_result["status"] = "failed"
                send_result["error"] = (
                    f"not sent: the attempt could not be recorded "
                    f"({type(e).__name__}: {e})"
                )
                send_result.setdefault("crm", "error")
                send_result["crm_error"] = str(e)
                result_summary["send_results"].append(send_result)
                continue

            # Send only now, with the claim on disk.
            simulated = workflow_sender is None
            sent_ok = False
            send_error = None
            if simulated:
                # Known here and lost before: "simulated" went out in the
                # response and nowhere else, so the row could not say whether
                # the mail ever left the process. ``release`` now writes it.
                result_summary["emails_sent"] += 1
                send_result["status"] = "simulated"
                sent_ok = True
            else:
                try:
                    from app.utils.email_sender import send_email as smtp_send
                    result = await smtp_send(
                        to_email=to_email,
                        to_name=company_name,
                        subject=subject,
                        body=body,
                        sender=workflow_sender,
                    )
                    if result.get("success"):
                        result_summary["emails_sent"] += 1
                        send_result["status"] = "sent"
                        sent_ok = True
                    else:
                        result_summary["emails_failed"] += 1
                        send_result["status"] = "failed"
                        send_result["error"] = result.get("error", "Unknown error")
                        send_error = send_result["error"]
                except Exception as e:
                    result_summary["emails_failed"] += 1
                    send_result["status"] = "failed"
                    send_result["error"] = str(e)
                    send_error = str(e)

            # Settle the claim. Through the same release the batch send uses,
            # so a row here carries the same meaning as a row there (sent_at is
            # written, a failure carries its reason, simulated is recorded).
            await release(
                db,
                email_log.id,
                sent=sent_ok,
                error=send_error,
                simulated=simulated and sent_ok,
            )

            result_summary["send_results"].append(send_result)

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
            # due_only: the scheduled job acts on *today's* plan. A commitment
            # made today is due later, so this is what stops the same customer
            # appearing in the notification every single morning.
            follow_result = await daily_follow_up_intelligence(db_session=db, due_only=True)
            report["follow_ups"] = follow_result.get("actions", [])
            report["high_priority_followups"] = follow_result.get("high_priority", 0)
            report["follow_ups_deferred"] = follow_result.get("deferred", 0)
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
    followups = len(report.get("follow_ups", []))
    if followups > 0:
        lines.append(f"[Follow-ups] {followups} due today:")
        for fu in report.get("follow_ups", [])[:3]:
            name = fu.get("company_name", "?")
            action = fu.get("suggested_action", "")
            kind = fu.get("kind", "")
            attempt = fu.get("attempt", 0)
            overdue = fu.get("days_overdue", 0)
            if kind == "awaiting_approval":
                # Not work for the agent: a draft is written and a person has to
                # decide. Saying "follow up" here would send someone to chase a
                # customer whose email is already waiting in their own queue.
                lines.append(f"  - [waiting on you] {name}: {action}")
                lines.append(f"      {fu.get('reason', '')}")
                continue
            suffix = f" (attempt {attempt})" if attempt else ""
            lines.append(f"  - [{kind}] {name}{suffix}: {action}")
            if overdue:
                lines.append(f"      {overdue} day(s) overdue")
        deferred = report.get("follow_ups_deferred", 0)
        if deferred:
            # Counted, not silently dropped: a truncated list reads as complete.
            lines.append(f"  (+{deferred} more due, over today's cap)")
        lines.append("")
    else:
        lines.append("[Follow-ups] Nothing is due today.")
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

    # A missing key is the same situation as a failed call: the caller has to
    # be able to tell the human reviewer that this is not model output. The
    # prompt above says "Don't sound like a template" and the fallback below is
    # one, which is why the result carries `ai_powered`.
    reason = None
    if is_configured():
        try:
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

            result = await complete_json(
                prompt,
                system="You write personalized B2B cold emails for a Chinese textile supplier. Each email must be 120-180 words, reference specific details, include product benefits, and a concrete next step. Return valid JSON only.",
                temperature=0.7,
                max_tokens=WRITING_MAX_TOKENS,
                scope="lead_draft_email",
            )

            return {
                "company_name": company_name,
                "to_email": emails[0],
                "subject": result.get("subject", f"Business inquiry - {product_keywords}"),
                "body": result.get("body", ""),
                "body_preview": result.get("body", "")[:100] + "...",
                "ai_powered": True,
                "fallback_reason": None,
            }
        except LLMTruncated:
            reason = "model response was cut off at the token cap"
        except Exception as e:
            reason = f"model call failed: {e}"
        logger.warning("draft email for %s fell back to a template: %s", company_name, reason)
    else:
        reason = "no_api_key"

    # Fallback: simple template
    return {
        "company_name": company_name,
        "to_email": emails[0],
        "subject": f"Quality {product_keywords} supplier - let's connect",
        "body": f"Dear {company_name},\n\nI noticed your company in {country} and thought our {product_keywords} might be a good fit for your business.\n\nWould you be open to a quick chat about how we can work together?\n\nBest regards",
        "body_preview": f"Dear {company_name}, I noticed your company in {country}...",
        "ai_powered": False,
        "fallback_reason": reason or "template_requested",
    }
