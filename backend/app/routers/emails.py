"""Email management API routes."""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.customer import Customer, CustomerContact
from app.models.email_template import EmailTemplate
from app.models.email_log import EmailLog, EmailStatus
from app.schemas.email_template import (
    EmailTemplateCreate, EmailTemplateUpdate, EmailTemplateResponse,
    AIGenerateRequest, AIGenerateResponse,
)
from app.schemas.email_log import (
    EmailSendRequest, EmailBatchSendRequest,
    EmailLogResponse, EmailStatsResponse,
)
from app.utils.email_sender import send_email, send_batch_emails, render_template, generate_tracking_id
from app.services.ai_service import generate_cold_email
from app.services.analytics_service import get_dashboard_stats

router = APIRouter(prefix="/emails", tags=["Emails"])


# --- Templates ---
@router.post("/templates", response_model=EmailTemplateResponse, status_code=201)
async def create_template(data: EmailTemplateCreate, db: AsyncSession = Depends(get_db)):
    """Create an email template."""
    template = EmailTemplate(**data.model_dump())
    db.add(template)
    await db.flush()
    await db.refresh(template)
    return template


@router.get("/templates", response_model=list[EmailTemplateResponse])
async def list_templates(
    category: str | None = None,
    language: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List email templates."""
    query = select(EmailTemplate)
    if category:
        query = query.where(EmailTemplate.category == category)
    if language:
        query = query.where(EmailTemplate.language == language)
    query = query.order_by(EmailTemplate.use_count.desc())
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/templates/{template_id}", response_model=EmailTemplateResponse)
async def get_template(template_id: int, db: AsyncSession = Depends(get_db)):
    """Get a template by ID."""
    result = await db.execute(select(EmailTemplate).where(EmailTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.put("/templates/{template_id}", response_model=EmailTemplateResponse)
async def update_template(
    template_id: int, data: EmailTemplateUpdate, db: AsyncSession = Depends(get_db)
):
    """Update a template."""
    result = await db.execute(select(EmailTemplate).where(EmailTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(template, field, value)
    template.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(template)
    return template


@router.delete("/templates/{template_id}", status_code=204)
async def delete_template(template_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a template."""
    result = await db.execute(select(EmailTemplate).where(EmailTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    await db.delete(template)
    await db.flush()


# --- AI Email Generation ---
@router.post("/generate", response_model=AIGenerateResponse)
async def generate_email(data: AIGenerateRequest):
    """Generate a cold outreach email using AI."""
    result = await generate_cold_email(
        product_name=data.product_name,
        target_industry=data.target_industry,
        target_country=data.target_country,
        company_name=data.company_name,
        selling_points=data.selling_points,
        tone=data.tone,
        language=data.language,
    )
    return result


# --- Send Email ---
@router.post("/send", response_model=EmailLogResponse)
async def send_single_email(data: EmailSendRequest, db: AsyncSession = Depends(get_db)):
    """Send a single email."""
    subject = data.subject
    body = data.body

    # If template is specified, render it
    if data.template_id:
        result = await db.execute(select(EmailTemplate).where(EmailTemplate.id == data.template_id))
        template = result.scalar_one_or_none()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        variables = data.variables or {}
        if data.customer_id:
            cust_result = await db.execute(select(Customer).where(Customer.id == data.customer_id))
            customer = cust_result.scalar_one_or_none()
            if customer:
                variables.setdefault("company_name", customer.company_name)
                variables.setdefault("country", customer.country or "")
                variables.setdefault("industry", customer.industry or "")
                if data.contact_id:
                    contact_result = await db.execute(
                        select(CustomerContact).where(CustomerContact.id == data.contact_id)
                    )
                    contact = contact_result.scalar_one_or_none()
                    if contact:
                        variables.setdefault("contact_name", contact.name)
                        variables.setdefault("contact_title", contact.title or "")
        subject = render_template(template.subject, variables)
        body = render_template(template.body, variables)
        template.use_count += 1

    if not subject or not body:
        raise HTTPException(status_code=400, detail="Subject and body are required (either directly or via template)")

    # Create tracking ID
    tracking_id = generate_tracking_id()

    # Send email
    send_result = await send_email(
        to_email=data.to_email,
        to_name=data.to_name or "",
        subject=subject,
        body=body,
        tracking_id=tracking_id,
    )

    # Log the email
    email_log = EmailLog(
        customer_id=data.customer_id,
        template_id=data.template_id,
        to_email=data.to_email,
        to_name=data.to_name,
        subject=subject,
        body=body,
        tracking_id=tracking_id,
        status=EmailStatus.SENT if send_result["success"] else EmailStatus.FAILED,
        error_message=send_result.get("error"),
        sent_at=datetime.utcnow() if send_result["success"] else None,
    )
    db.add(email_log)
    await db.flush()
    await db.refresh(email_log)
    return email_log


# --- Email Logs ---
@router.get("/logs", response_model=list[EmailLogResponse])
async def list_email_logs(
    customer_id: int | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List email logs."""
    query = select(EmailLog).order_by(EmailLog.created_at.desc()).limit(100)
    if customer_id:
        query = query.where(EmailLog.customer_id == customer_id)
    if status:
        query = query.where(EmailLog.status == status)
    result = await db.execute(query)
    return list(result.scalars().all())


# --- Email Stats ---
@router.get("/stats", response_model=EmailStatsResponse)
async def get_email_stats(db: AsyncSession = Depends(get_db)):
    """Get email statistics."""
    from sqlalchemy import func

    total_result = await db.execute(select(func.count(EmailLog.id)))
    total_sent = total_result.scalar() or 0

    opened_result = await db.execute(
        select(func.count(EmailLog.id)).where(
            EmailLog.status.in_([EmailStatus.OPENED, EmailStatus.REPLIED])
        )
    )
    total_opened = opened_result.scalar() or 0

    replied_result = await db.execute(
        select(func.count(EmailLog.id)).where(EmailLog.status == EmailStatus.REPLIED)
    )
    total_replied = replied_result.scalar() or 0

    bounced_result = await db.execute(
        select(func.count(EmailLog.id)).where(EmailLog.status == EmailStatus.BOUNCED)
    )
    total_bounced = bounced_result.scalar() or 0

    failed_result = await db.execute(
        select(func.count(EmailLog.id)).where(EmailLog.status == EmailStatus.FAILED)
    )
    total_failed = failed_result.scalar() or 0

    return EmailStatsResponse(
        total_sent=total_sent,
        total_opened=total_opened,
        total_replied=total_replied,
        total_bounced=total_bounced,
        total_failed=total_failed,
        open_rate=round(total_opened / total_sent * 100, 1) if total_sent > 0 else 0,
        reply_rate=round(total_replied / total_sent * 100, 1) if total_sent > 0 else 0,
    )


# --- Tracking Pixel ---
@router.get("/track/{tracking_id}/pixel")
async def track_email_open(tracking_id: str, db: AsyncSession = Depends(get_db)):
    """Track email open via invisible pixel."""
    result = await db.execute(
        select(EmailLog).where(EmailLog.tracking_id == tracking_id)
    )
    email_log = result.scalar_one_or_none()
    if email_log and email_log.status not in (EmailStatus.REPLIED, EmailStatus.BOUNCED):
        email_log.status = EmailStatus.OPENED
        email_log.open_count += 1
        if not email_log.opened_at:
            email_log.opened_at = datetime.utcnow()
        await db.flush()

    # Return 1x1 transparent GIF pixel
    pixel = (
        b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00"
        b"\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00"
        b"\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02"
        b"\x44\x01\x00\x3b"
    )
    return Response(content=pixel, media_type="image/gif")