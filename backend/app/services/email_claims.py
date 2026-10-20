"""Claiming an email row before sending it, and replaying a request safely.

Idempotency, three layers, in order of what actually holds:

1. **The row's status is the lock.** ``PENDING -> SENDING`` is a single atomic
   ``UPDATE ... WHERE status = PENDING``. Two workers -- two requests, two
   processes, or the same one after a crash -- cannot both win it, because the
   second sees no row to update. This is the layer correctness depends on.
2. **``tracking_id`` is the idempotency key.** It is ``UNIQUE``, so a caller who
   supplies the same key twice gets the same row and, if that row already went
   out, the original answer instead of a second email.
3. **A process-local lock** (in the router) refuses an obviously concurrent
   second send with a readable 409 before any work is done. It is a courtesy,
   not the guarantee: it cannot coordinate across processes, and it does not
   survive a crash.

What is deliberately *not* claimed: exactly-once delivery. If the process dies
between the SMTP handoff and the status write, the mail may or may not have
arrived, and nothing in this file can find out. Such a row is left as
``SENDING`` until :func:`mark_stale_sends` resolves it to ``FAILED`` with the
word "unknown" in the message, so a human decides. Guessing there would mean
either emailing a customer twice or silently dropping a send.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_log import EmailLog, EmailStatus, SETTLED_SEND_STATES

logger = logging.getLogger(__name__)

#: How long a claim may sit unresolved before it is treated as abandoned. A
#: claim is held across exactly one SMTP call, so this is generous on purpose:
#: a slow mail server must not make a live attempt look abandoned.
STALE_CLAIM_SECONDS = 900


def idempotency_tracking_id(key: str) -> str:
    """A stable tracking id for a caller-supplied idempotency key.

    The schema has no column for the caller's key, and adding one is not
    possible on an existing database (the app creates tables, it does not
    migrate them). It does not need one: ``tracking_id`` is already unique and
    already travels with the send, so deriving it from the key gives the same
    replay protection with the columns that exist.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"idem-{digest}"


async def claim(db: AsyncSession, email_id: int) -> bool:
    """Take exclusive ownership of one pending row. True if we got it.

    The condition is in the ``WHERE`` clause, not in a read-then-write in
    Python: reading the status and then updating it is exactly the race this
    exists to prevent.
    """
    result = await db.execute(
        update(EmailLog)
        .where(EmailLog.id == email_id, EmailLog.status == EmailStatus.PENDING)
        .values(status=EmailStatus.SENDING)
    )
    await db.commit()
    return result.rowcount == 1


async def release(
    db: AsyncSession,
    email_id: int,
    *,
    sent: bool,
    error: str | None = None,
    unknown: bool = False,
    simulated: bool = False,
) -> None:
    """Finish a claim: the row moves to a settled state, or back to pending.

    ``unknown`` is for the one case that cannot be resolved: the attempt was
    made and its outcome is not knowable. The row goes to ``FAILED`` with a
    message that says so, rather than being quietly retried.

    ``simulated`` records that the "send" never left the process -- the sender
    had no SMTP credentials and reported success for a message it did not hand
    to any server. The status is still ``SENT`` (the flows that follow a send
    treat it like any other outbound message), but the row must carry the
    difference, or "the mail was sent but never arrived" cannot even be
    triaged.
    """
    row = (
        await db.execute(select(EmailLog).where(EmailLog.id == email_id))
    ).scalar_one_or_none()
    if row is None:
        return

    if sent:
        row.status = EmailStatus.SENT
        row.sent_at = datetime.utcnow()
        row.error_message = None
        row.simulated = bool(simulated)
    else:
        row.status = EmailStatus.FAILED
        row.error_message = error or "Unknown error"
        if unknown:
            row.error_message = (
                f"outcome unknown: {row.error_message}. The attempt was made and "
                "the result could not be recorded; check the mailbox before "
                "resending."
            )
    await db.commit()


async def mark_stale_sends(
    db: AsyncSession, *, older_than_seconds: int = STALE_CLAIM_SECONDS
) -> int:
    """Resolve the claims a dead process left behind.

    A row stuck in ``SENDING`` is not "about to be sent" -- the process that
    claimed it is gone. It is not re-queued either: whether that mail arrived is
    genuinely unknown, and re-queueing it would risk a duplicate while quietly
    re-sending is exactly what the claim existed to prevent.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=older_than_seconds)
    rows = (
        await db.execute(
            select(EmailLog).where(
                EmailLog.status == EmailStatus.SENDING,
                EmailLog.created_at < cutoff,
            )
        )
    ).scalars().all()

    for row in rows:
        row.status = EmailStatus.FAILED
        row.error_message = (
            "outcome unknown: the process that claimed this send exited before "
            "recording the result. Check the mailbox before resending."
        )
    if rows:
        await db.commit()
        logger.warning("resolved %d abandoned email claim(s)", len(rows))
    return len(rows)


async def already_handled(db: AsyncSession, tracking_id: str) -> EmailLog | None:
    """The row for this idempotency key, if this request has been seen before."""
    return (
        await db.execute(select(EmailLog).where(EmailLog.tracking_id == tracking_id))
    ).scalar_one_or_none()


def as_replay(row: EmailLog) -> EmailLog:
    """Mark the row as the answer to a repeated request.

    The two flags are *not* columns: they are read off the object by the response
    schema, and they do not exist on a freshly sent row, where the schema
    defaults apply. Returning the row itself rather than a hand-built dict is
    deliberate -- a hand-built dict is a second copy of the response shape, and
    the two drift (this file's first version was missing ``body``, ``open_count``
    and ``created_at``, which the schema then rejected at runtime).
    """
    row.replayed = True
    row.already_sent = row.status in SETTLED_SEND_STATES
    return row
