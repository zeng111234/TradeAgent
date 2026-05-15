"""Automated workflows - the 'brain' that connects all tools.

Instead of you operating each tool, workflows run them automatically
and compile results into a human-readable report.
"""
import logging
import asyncio
from datetime import datetime

from app.notification import send_notification

logger = logging.getLogger(__name__)


async def daily_morning_routine(search_keywords: str = "textile buyer", target_country: str = "") -> dict:
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

    # Step 1: Scan for new leads
    logger.info("Step 1: Scanning for new leads...")
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