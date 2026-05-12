"""Analytics and dashboard service."""
from datetime import datetime, timedelta
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer, CustomerStage
from app.models.email_log import EmailLog, EmailStatus
from app.models.task import Task, TaskStatus


async def get_dashboard_stats(db: AsyncSession) -> dict:
    """Get main dashboard statistics."""
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Total customers
    total_result = await db.execute(select(func.count(Customer.id)))
    total_customers = total_result.scalar() or 0

    # New customers this month
    new_result = await db.execute(
        select(func.count(Customer.id)).where(Customer.created_at >= month_start)
    )
    new_customers = new_result.scalar() or 0

    # Email stats
    total_emails_result = await db.execute(select(func.count(EmailLog.id)))
    total_emails_sent = total_emails_result.scalar() or 0

    month_emails_result = await db.execute(
        select(func.count(EmailLog.id)).where(EmailLog.created_at >= month_start)
    )
    emails_this_month = month_emails_result.scalar() or 0

    # Open rate
    opened_result = await db.execute(
        select(func.count(EmailLog.id)).where(EmailLog.status.in_([
            EmailStatus.OPENED, EmailStatus.REPLIED
        ]))
    )
    total_opened = opened_result.scalar() or 0
    open_rate = (total_opened / total_emails_sent * 100) if total_emails_sent > 0 else 0

    # Reply rate
    replied_result = await db.execute(
        select(func.count(EmailLog.id)).where(EmailLog.status == EmailStatus.REPLIED)
    )
    total_replied = replied_result.scalar() or 0
    reply_rate = (total_replied / total_emails_sent * 100) if total_emails_sent > 0 else 0

    # Pending tasks
    pending_result = await db.execute(
        select(func.count(Task.id)).where(Task.status == TaskStatus.PENDING)
    )
    pending_tasks = pending_result.scalar() or 0

    # Overdue tasks
    overdue_result = await db.execute(
        select(func.count(Task.id)).where(
            Task.status == TaskStatus.PENDING,
            Task.due_date < now,
        )
    )
    overdue_tasks = overdue_result.scalar() or 0

    return {
        "total_customers": total_customers,
        "new_customers_this_month": new_customers,
        "total_emails_sent": total_emails_sent,
        "emails_sent_this_month": emails_this_month,
        "open_rate": round(open_rate, 1),
        "reply_rate": round(reply_rate, 1),
        "pending_tasks": pending_tasks,
        "overdue_tasks": overdue_tasks,
    }


async def get_pipeline_funnel(db: AsyncSession) -> dict:
    """Get customer pipeline funnel data."""
    total_result = await db.execute(select(func.count(Customer.id)))
    total = total_result.scalar() or 0

    stages = []
    stage_names = [
        ("new", "New"),
        ("contacted", "Contacted"),
        ("interested", "Interested"),
        ("quoting", "Quoting"),
        ("sample", "Sample"),
        ("ordering", "Ordering"),
        ("completed", "Completed"),
        ("lost", "Lost"),
    ]

    for stage_value, stage_label in stage_names:
        result = await db.execute(
            select(func.count(Customer.id)).where(Customer.stage == stage_value)
        )
        count = result.scalar() or 0
        percentage = (count / total * 100) if total > 0 else 0
        stages.append({
            "stage": stage_label,
            "count": count,
            "percentage": round(percentage, 1),
        })

    return {"stages": stages, "total": total}


async def get_source_stats(db: AsyncSession) -> list[dict]:
    """Get customer acquisition source statistics."""
    total_result = await db.execute(select(func.count(Customer.id)))
    total = total_result.scalar() or 0

    result = await db.execute(
        select(Customer.source, func.count(Customer.id))
        .group_by(Customer.source)
    )

    stats = []
    for source, count in result.all():
        percentage = (count / total * 100) if total > 0 else 0
        stats.append({
            "source": source.value if source else "unknown",
            "count": count,
            "percentage": round(percentage, 1),
        })
    return stats


async def get_country_stats(db: AsyncSession) -> list[dict]:
    """Get customer distribution by country."""
    result = await db.execute(
        select(Customer.country, func.count(Customer.id))
        .where(Customer.country.isnot(None))
        .group_by(Customer.country)
        .order_by(func.count(Customer.id).desc())
        .limit(20)
    )
    return [{"country": c, "count": n} for c, n in result.all()]


async def get_monthly_trends(db: AsyncSession, months: int = 6) -> list[dict]:
    """Get monthly trend data."""
    now = datetime.utcnow()
    trends = []

    for i in range(months - 1, -1, -1):
        month_start = (now - timedelta(days=30 * i)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if i > 0:
            month_end = (now - timedelta(days=30 * (i - 1))).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            month_end = now

        # Customers added
        cust_result = await db.execute(
            select(func.count(Customer.id)).where(
                Customer.created_at >= month_start,
                Customer.created_at < month_end,
            )
        )
        customers_added = cust_result.scalar() or 0

        # Emails sent
        email_result = await db.execute(
            select(func.count(EmailLog.id)).where(
                EmailLog.created_at >= month_start,
                EmailLog.created_at < month_end,
            )
        )
        emails_sent = email_result.scalar() or 0

        # Emails opened
        opened_result = await db.execute(
            select(func.count(EmailLog.id)).where(
                EmailLog.created_at >= month_start,
                EmailLog.created_at < month_end,
                EmailLog.status.in_([EmailStatus.OPENED, EmailStatus.REPLIED]),
            )
        )
        emails_opened = opened_result.scalar() or 0

        # Emails replied
        replied_result = await db.execute(
            select(func.count(EmailLog.id)).where(
                EmailLog.created_at >= month_start,
                EmailLog.created_at < month_end,
                EmailLog.status == EmailStatus.REPLIED,
            )
        )
        emails_replied = replied_result.scalar() or 0

        trends.append({
            "month": month_start.strftime("%Y-%m"),
            "customers_added": customers_added,
            "emails_sent": emails_sent,
            "emails_opened": emails_opened,
            "emails_replied": emails_replied,
        })

    return trends