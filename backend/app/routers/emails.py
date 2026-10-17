"""Email management API routes.

Sending is the one operation here that cannot be undone, so it is guarded at
three levels -- see ``app/services/email_claims.py`` for the reasoning:

* the row's own status is the lock (``pending -> sending`` is one atomic
  ``UPDATE ... WHERE status = pending``);
* ``tracking_id`` is the idempotency key, so a replay of the same request is
  answered from the record instead of sending again;
* a process-local lock refuses an obviously concurrent second send early, with a
  readable error, before any work happens.

A draft is only sendable from ``pending``. That single rule is what makes
"the same draft cannot go out twice" true rather than hoped for.
"""
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.customer import Customer, CustomerContact
from app.models.email_template import EmailTemplate
from app.models.email_log import EmailLog, EmailStatus, SETTLED_SEND_STATES
from app.schemas.email_template import (
    EmailTemplateCreate, EmailTemplateUpdate, EmailTemplateResponse,
    AIGenerateRequest, AIGenerateResponse,
)
from app.schemas.email_log import (
    EmailSendRequest, EmailBatchSendRequest,
    EmailLogResponse, EmailStatsResponse,
)
from app.timeouts import BATCH_SEND_BUDGET_SECONDS
from app.services.email_claims import (
    already_handled,
    claim,
    idempotency_tracking_id,
    mark_stale_sends,
    release,
    as_replay,
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
async def send_single_email(
    data: EmailSendRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Send a single email.

    Pass ``Idempotency-Key`` and the same request twice sends one email: the
    tracking id is derived from the key, so the second call finds the first
    call's row and answers from the record instead of sending again. Without a
    key there is nothing to recognise a replay by, and the row is still claimed
    before the send so a concurrent double-click cannot fire twice.

    The row is written **before** the mail is handed to SMTP. That ordering is
    the point: a crash between "sent" and "logged" used to be invisible and the
    mail was sent again on the next attempt.
    """
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

    # The idempotency key, if given, decides the tracking id -- and therefore
    # decides that a replay is recognisable.
    tracking_id = (
        idempotency_tracking_id(idempotency_key)
        if (idempotency_key or "").strip()
        else generate_tracking_id()
    )

    seen = await already_handled(db, tracking_id)
    if seen is not None:
        if seen.status in SETTLED_SEND_STATES:
            response.status_code = 200
            return as_replay(seen)
        if seen.status == EmailStatus.SENDING:
            # In flight, or abandoned mid-flight. Either way the outcome is not
            # knowable from here, and sending again is the one thing that could
            # make it worse.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"an attempt for this key is already in flight (email log "
                    f"{seen.id}, status sending). Its outcome is not known yet; "
                    "wait for it, or check the mailbox before retrying."
                ),
            )
        if seen.status == EmailStatus.FAILED:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a previous attempt with this key failed (email log {seen.id}): "
                    f"{seen.error_message}. Retry with a new Idempotency-Key once you "
                    "have decided it is safe to send again."
                ),
            )

    # Claim before sending. The row exists (status `sending`) before the mail is
    # handed over, so a crash in between leaves evidence instead of a hole.
    claim_row = EmailLog(
        customer_id=data.customer_id,
        template_id=data.template_id,
        to_email=data.to_email,
        to_name=data.to_name,
        subject=subject,
        body=body,
        tracking_id=tracking_id,
        status=EmailStatus.SENDING,
    )
    db.add(claim_row)
    await db.commit()
    await db.refresh(claim_row)

    # Send email
    send_result = await send_email(
        to_email=data.to_email,
        to_name=data.to_name or "",
        subject=subject,
        body=body,
        tracking_id=tracking_id,
    )

    await release(
        db,
        claim_row.id,
        sent=bool(send_result["success"]),
        error=send_result.get("error"),
    )
    await db.refresh(claim_row)

    return claim_row


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


# --- Sending is serialised -------------------------------------------------

#: One send at a time, for the whole process.
#:
#: Both send endpoints do read -> ``await send_email(...)`` -> write, with
#: nothing claimed in between: the read and the write are separated by the whole
#: SMTP round trip, and the ``status == PENDING`` check happens *before* that
#: await, so it guards nothing. Two overlapping calls therefore both read the
#: same PENDING rows and both send them. Observed: ten drafts, two concurrent
#: calls, twenty SMTP calls, every customer emailed the same thing twice --
#: while both calls reported success and every row ended up ``sent``, so the
#: duplication left no trace in the record.
#:
#: That observation does not reproduce on demand: whether the second call's read
#: lands inside the first call's send window is a matter of scheduling. The
#: *mechanism* is unconditional, which is the part that matters -- the exposure
#: is real whenever two sends overlap, not only when a stopwatch cooperates.
#:
#: A ``threading.Lock`` acquired non-blockingly, not an ``asyncio.Lock``. Three
#: reasons, and the third is the one that bit:
#:
#: * ``acquire(blocking=False)`` is a single atomic test-and-set, so there is no
#:   gap between deciding the lock is free and taking it.
#: * It never blocks the event loop, because the caller is refused rather than
#:   queued.
#: * An ``asyncio.Lock`` is **bound to the event loop that first contends on
#:   it**, and raises ``RuntimeError`` when another loop does. That is invisible
#:   in production, where one loop lives for the life of the process, and it is
#:   a trap everywhere else -- in tests it turned into a silently skipped run.
#:
#: A claim row in the database is what actually protects a send (the
#: ``pending -> sending`` transition in ``app/services/email_claims.py``); this
#: lock is a courtesy that refuses an obviously concurrent second send before any
#: work is done. It does not coordinate across processes -- the claim does.
_SEND_LOCK = threading.Lock()


@asynccontextmanager
async def _exclusive_send(what: str):
    """Refuse a second concurrent send rather than queueing it.

    Refusing is the honest answer: the caller asked to send these emails, and
    they are already being sent. Queueing would hold the request open for the
    length of a batch, which for a hundred customers is minutes.
    """
    if not _SEND_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=(
                f"another send is already in progress; {what} was not started. "
                "Wait for it to finish -- sending the same drafts twice would "
                "email those customers twice."
            ),
        )
    try:
        yield
    finally:
        _SEND_LOCK.release()


# --- Batch Send Pending Drafts ---
#: How many drafts one batch send will attempt. A knob, not a discovery: it is
#: set so a single request finishes inside a normal HTTP timeout for the SMTP
#: server this is built against (roughly a second per message), and it is the
#: number to raise if the mail server is faster.
DEFAULT_BATCH_LIMIT = 50


@router.post("/send-pending")
async def api_send_pending(
    limit: int = DEFAULT_BATCH_LIMIT,
    db: AsyncSession = Depends(get_db),
):
    """Send pending drafts, oldest first, in a bounded batch.

    This used to send **every** pending draft in one request. With a queue of a
    thousand that is one request holding the send lock for as long as the mail
    server takes -- minutes -- during which every other send is refused with a
    409, the client times out, and the operator has no idea how far it got. The
    work is resumable (each row is claimed and committed before its own send, see
    ``app/services/email_claims.py``), so the honest shape is a bounded batch:
    the caller repeats until ``remaining`` is zero, and sees the queue shrink.

    Ordered by ``created_at``, oldest first, so a backlog drains in the order it
    built up instead of whatever order the database felt like returning.
    """
    if limit < 1:
        raise HTTPException(status_code=422, detail="limit must be at least 1")
    limit = min(limit, 500)

    async with _exclusive_send("the batch send"):
        # Re-read *inside* the lock. The set of pending drafts is the thing being
        # claimed, so it has to be read after the claim, not before it.
        #
        # `remaining` is counted before claiming anything, so the caller learns
        # the queue depth from the same request that drains part of it instead of
        # needing a second call to find out.
        remaining_before = (
            await db.execute(
                select(func.count(EmailLog.id)).where(
                    EmailLog.status == EmailStatus.PENDING
                )
            )
        ).scalar_one()

        if not remaining_before:
            return {
                "sent": 0, "failed": 0, "skipped": 0,
                "total": 0, "remaining": 0, "limit": limit,
                "message": "No pending drafts to send.",
            }

        result = await db.execute(
            select(EmailLog)
            .where(EmailLog.status == EmailStatus.PENDING)
            .order_by(EmailLog.created_at, EmailLog.id)
            .limit(limit)
        )
        pending = list(result.scalars().all())

        sent_count = 0
        failed_count = 0
        skipped_count = 0
        details = []
        deadline = time.monotonic() + BATCH_SEND_BUDGET_SECONDS
        stopped_on_time = False

        for email_log in pending:
            # Two bounds, not one. The count (`limit`) keeps a single request from
            # being enormous; this clock keeps it from *lasting* arbitrarily long,
            # because each message is allowed SMTP_SEND_SECONDS and thirty
            # seconds times fifty drafts is twenty-five minutes inside a
            # process-wide lock. Whichever bound is reached first ends the batch,
            # and the response says how many are left so the caller can go again.
            if time.monotonic() >= deadline:
                stopped_on_time = True
                break

            # Claim this row, and commit the claim, *before* sending it.
            #
            # The old loop sent every draft and committed once at the end. A
            # crash in the middle therefore lost the status of every mail that
            # had already gone out, leaving them all as `pending` for the next
            # run to send again -- ten customers, twenty emails. The claim is
            # what makes a crash cost at most the one in-flight attempt, whose
            # outcome mark_stale_sends then reports as unknown rather than
            # guessing.
            if not await claim(db, email_log.id):
                skipped_count += 1
                details.append({
                    "to_email": email_log.to_email,
                    "subject": email_log.subject,
                    "success": False,
                    "error": "claimed by another send",
                })
                continue

            send_result = await send_email(
                to_email=email_log.to_email,
                to_name=email_log.to_name or "",
                subject=email_log.subject,
                body=email_log.body,
                tracking_id=email_log.tracking_id,
            )

            await release(
                db,
                email_log.id,
                sent=bool(send_result["success"]),
                error=send_result.get("error"),
            )

            if send_result["success"]:
                sent_count += 1
            else:
                failed_count += 1

            details.append({
                "to_email": email_log.to_email,
                "subject": email_log.subject,
                "success": send_result["success"],
                "error": send_result.get("error"),
            })

        remaining_after = (
            await db.execute(
                select(func.count(EmailLog.id)).where(
                    EmailLog.status == EmailStatus.PENDING
                )
            )
        ).scalar_one()

        return {
            "sent": sent_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "total": sent_count + failed_count + skipped_count,
            "limit": limit,
            # The queue depth *after* this batch, so a caller can loop until it
            # reaches zero instead of guessing how many calls it needs.
            "remaining": remaining_after,
            "queue_before": remaining_before,
            # True when the clock, not the count, ended the batch. The caller
            # learns "come back for the rest" instead of "this is everything".
            "stopped_on_time": stopped_on_time,
            "budget_seconds": BATCH_SEND_BUDGET_SECONDS,
            "details": details,
        }


@router.post("/send-pending/{email_id}")
async def api_send_single_pending(email_id: int, db: AsyncSession = Depends(get_db)):
    """Send a single pending draft email by its ID.

    A second click on the same draft is refused rather than queued: the button
    is a request to send *this* email, and it is already being sent.
    """
    async with _exclusive_send("this email"):
        result = await db.execute(select(EmailLog).where(EmailLog.id == email_id))
        email_log = result.scalar_one_or_none()

        if not email_log:
            raise HTTPException(status_code=404, detail="Email log not found")
        if email_log.status != EmailStatus.PENDING:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Email is not pending (status: {email_log.status}). "
                    "A draft can only be sent from pending, which is what makes "
                    "sending it twice impossible."
                ),
            )

        # Same claim as the batch path, so the two cannot both send this row --
        # the check above is a readable error, this is the guarantee.
        if not await claim(db, email_log.id):
            raise HTTPException(
                status_code=409,
                detail="this draft was claimed by another send",
            )

        send_result = await send_email(
            to_email=email_log.to_email,
            to_name=email_log.to_name or "",
            subject=email_log.subject,
            body=email_log.body,
            tracking_id=email_log.tracking_id,
        )

        await release(
            db,
            email_log.id,
            sent=bool(send_result["success"]),
            error=send_result.get("error"),
        )
        await db.refresh(email_log)
        return email_log


@router.put("/logs/{email_id}")
async def api_update_email_log(email_id: int, data: dict, db: AsyncSession = Depends(get_db)):
    """Update a pending email log (edit subject/body before sending)."""
    result = await db.execute(select(EmailLog).where(EmailLog.id == email_id))
    email_log = result.scalar_one_or_none()

    if not email_log:
        raise HTTPException(status_code=404, detail="Email log not found")

    if "subject" in data:
        email_log.subject = data["subject"]
    if "body" in data:
        email_log.body = data["body"]
    if "to_email" in data:
        email_log.to_email = data["to_email"]

    await db.commit()
    await db.refresh(email_log)
    return email_log


@router.delete("/logs/{email_id}", status_code=204)
async def api_delete_email_log(email_id: int, db: AsyncSession = Depends(get_db)):
    """Delete an email log entry (remove a pending draft)."""
    result = await db.execute(select(EmailLog).where(EmailLog.id == email_id))
    email_log = result.scalar_one_or_none()

    if not email_log:
        raise HTTPException(status_code=404, detail="Email log not found")

    await db.delete(email_log)
    await db.flush()


# --- Recording a reply ---

class ReplyRecordRequest(BaseModel):
    """When the customer answered, if it is not now."""
    replied_at: Optional[datetime] = Field(
        None, description="When they replied. Defaults to now."
    )


@router.post("/logs/{email_id}/reply")
async def api_record_reply(
    email_id: int,
    data: Optional[ReplyRecordRequest] = None,
    db: AsyncSession = Depends(get_db),
):
    """Record that the customer answered **this** email.

    The only write path into ``EmailStatus.REPLIED``. Until it existed the state
    was declared, counted by ``/emails/stats`` as ``reply_rate``, read by the
    agent's tools, and impossible to enter: no endpoint, workflow or agent tool
    ever set ``replied_at``. So ``reply_rate`` was 0.0% by construction, and
    every "3 emails, no reply" alert described a state the system could not tell
    apart from a customer who had written back.

    Two properties worth knowing:

    * **Idempotent.** Recording the same reply twice does not move the
      timestamp. A reply happened once; a second call is a no-op that says so.
    * **It does not change the customer's stage.** A reply is contact, not a
      stage transition, and moving ``contacted`` to ``interested`` on the
      system's own initiative would be a different guess dressed as a fact.
    """
    result = await db.execute(select(EmailLog).where(EmailLog.id == email_id))
    email_log = result.scalar_one_or_none()
    if email_log is None:
        raise HTTPException(status_code=404, detail="Email log not found")

    if email_log.status in (EmailStatus.PENDING, EmailStatus.FAILED, EmailStatus.BOUNCED):
        raise HTTPException(
            status_code=409,
            detail=(
                f"this email was never delivered (status '{email_log.status.value}'), "
                "so it cannot have been answered"
            ),
        )

    supplied = data.replied_at if data else None
    when = supplied or datetime.utcnow()
    if when > datetime.utcnow():
        raise HTTPException(
            status_code=422,
            detail="replied_at cannot be in the future",
        )
    was_sent_at = email_log.sent_at or email_log.created_at
    if was_sent_at is not None and when < was_sent_at:
        # Refused rather than stored: a reply older than the email it answers
        # makes the whole conversation derivation nonsense -- every message
        # would look like it was sent after the reply, so none of them would
        # count as awaiting one, and the thread would read as settled.
        raise HTTPException(
            status_code=422,
            detail=(
                f"replied_at ({when.isoformat()}) is before this email was sent "
                f"({was_sent_at.isoformat()}); a reply cannot answer an email that "
                "had not been sent yet"
            ),
        )

    if supplied is None and was_sent_at is not None and when <= was_sent_at:
        # Clamp "now" to be strictly after the email it answers.
        #
        # The clock here cannot be trusted to order two events that happen close
        # together: Windows resolves `datetime.utcnow()` to roughly 15 ms, so a
        # send and the reply recorded a moment later can carry the *same*
        # timestamp. The conversation derivation asks whether the reply is newer
        # than the last outbound message, so a tie reads as "we spoke last" and
        # the customer who just answered is treated as silent -- which is exactly
        # what made a reply-endpoint test fail intermittently.
        #
        # A reply to an email cannot precede it, so the honest value is "just
        # after that email". Only the defaulted timestamp is adjusted; a time the
        # caller stated is taken at its word or refused above.
        when = was_sent_at + timedelta(microseconds=1)

    already = email_log.replied_at is not None
    if not already:
        email_log.replied_at = when
        email_log.status = EmailStatus.REPLIED

        # Contact happened, so the silence clock restarts: whoever spoke last,
        # the gap since the last exchange is now measured from here. The fact
        # that we still owe them an answer is carried by the thread state
        # below, not by leaving this timestamp stale.
        if email_log.customer_id is not None:
            customer = (
                await db.execute(
                    select(Customer).where(Customer.id == email_log.customer_id)
                )
            ).scalar_one_or_none()
            if customer is not None:
                if customer.last_contacted_at is None or customer.last_contacted_at < when:
                    customer.last_contacted_at = when
                # The chase is over; whether a new one starts is a decision, not
                # a timestamp left over from before they answered.
                customer.next_follow_up_at = None
        await db.commit()
        await db.refresh(email_log)

    return {
        "email_id": email_log.id,
        "recorded": not already,
        "already_recorded": already,
        "replied_at": email_log.replied_at.isoformat() if email_log.replied_at else None,
        "status": email_log.status.value,
        "customer_id": email_log.customer_id,
        "conversation": await _conversation_for(db, email_log),
    }


async def _conversation_for(db: AsyncSession, email_log: EmailLog) -> dict:
    """The derived thread state, so the caller sees the effect immediately."""
    if email_log.customer_id is None:
        return {}
    from app.services.follow_up_policy import derive_thread_state

    rows = (
        await db.execute(
            select(EmailLog).where(EmailLog.customer_id == email_log.customer_id)
        )
    ).scalars().all()
    return derive_thread_state(rows, datetime.utcnow()).as_dict()


# --- Rejecting a proposal ---

class RejectRequest(BaseModel):
    """Why the suggestion was turned down, if the human wants to say."""
    reason: Optional[str] = Field(
        None, max_length=500,
        description="Optional. Recorded on the row so the next proposal can see it.",
    )


@router.post("/logs/{email_id}/reject")
async def api_reject_draft(
    email_id: int,
    data: Optional[RejectRequest] = None,
    db: AsyncSession = Depends(get_db),
):
    """Turn down a draft that is waiting for approval.

    **Kept, not deleted.** A draft sitting in `pending` is a proposal, and a
    proposal that a human refused is information: the agent's next run needs to
    know that this approach was already turned down, and a deleted row carries
    none of that. Deleting is still available for a draft you wrote yourself and
    simply do not want (`DELETE /logs/{id}`); rejecting is the decision, and it
    leaves a record.

    Idempotent: rejecting twice does not change the first decision, and a draft
    that was already sent cannot be rejected.
    """
    result = await db.execute(select(EmailLog).where(EmailLog.id == email_id))
    email_log = result.scalar_one_or_none()
    if email_log is None:
        raise HTTPException(status_code=404, detail="Email log not found")

    if email_log.status == EmailStatus.REJECTED:
        return {
            "email_id": email_log.id,
            "rejected": False,
            "already_rejected": True,
            "reason": email_log.error_message,
            "customer_id": email_log.customer_id,
            "conversation": await _conversation_for(db, email_log),
        }

    if email_log.status != EmailStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=(
                f"only a draft waiting for approval can be rejected; this one is "
                f"'{email_log.status.value}'"
            ),
        )

    email_log.status = EmailStatus.REJECTED
    # `error_message` is the row's only free-text field, and for a rejected
    # proposal it holds the human's reason. Named for its other use, kept
    # because adding a column is not possible here -- see the note in
    # docs/问答记录.md Q7.
    email_log.error_message = (data.reason if data else None) or "rejected by a human"
    await db.commit()
    await db.refresh(email_log)

    return {
        "email_id": email_log.id,
        "rejected": True,
        "already_rejected": False,
        "reason": email_log.error_message,
        "customer_id": email_log.customer_id,
        "conversation": await _conversation_for(db, email_log),
    }


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