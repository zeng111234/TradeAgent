"""The scan-and-send workflow must obey the same claim shape as the batch send.

``app/routers/emails.py`` sends a batch by claiming each row and committing the
claim *before* the message leaves the process (``app/services/email_claims.py``).
``send_drafted_emails_workflow`` did not: it sent every message and wrote the
customer, the contact and the log rows in **one commit at the end**. A crash
mid-batch therefore left every mail that had already gone out with no row at
all -- and the next run, finding no customer and no log, sent them again. These
tests pin the three-step shape -- record, send, settle -- on that path too.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

import app.workflows as workflows
from app.config import settings as app_config
from app.models.email_log import EmailLog, EmailStatus


def lead_for(tag: str, **extra):
    """One selected lead with a website no other test is using."""
    suffix = uuid.uuid4().hex[:8]
    site = f"https://{tag}-{suffix}.example.test"
    lead = {
        "company_name": f"{tag} Ltd {suffix}",
        "country": "Germany",
        "website": site,
        "email": f"buyer-{suffix}@example.test",
        "draft_subject": f"Hello from {tag}",
        "draft_body": "<p>body</p>",
    }
    lead.update(extra)
    return lead


async def rows_for(website: str):
    """Read the customer and its log rows through a *fresh* session."""
    from app.database import async_session
    from app.models.customer import Customer

    async with async_session() as fresh:
        customer = (
            await fresh.execute(select(Customer).where(Customer.website == website))
        ).scalar_one_or_none()
        if customer is None:
            return None, []
        logs = list(
            (
                await fresh.execute(
                    select(EmailLog).where(EmailLog.customer_id == customer.id)
                )
            ).scalars().all()
        )
        return customer, logs


async def drop_for(website: str) -> None:
    """Remove this test's customer and mails; the book is shared."""
    from app.database import async_session
    from app.models.customer import Customer

    async with async_session() as fresh:
        customer = (
            await fresh.execute(select(Customer).where(Customer.website == website))
        ).scalar_one_or_none()
        if customer is None:
            return
        for log in (
            await fresh.execute(
                select(EmailLog).where(EmailLog.customer_id == customer.id)
            )
        ).scalars().all():
            await fresh.delete(log)
        await fresh.delete(customer)
        await fresh.commit()


def smtp_on(monkeypatch):
    """Route the workflow down the real send branch (tests have no SMTP)."""
    monkeypatch.setattr(app_config, "SMTP_USER", "user@example.test", raising=False)
    monkeypatch.setattr(app_config, "SMTP_PASSWORD", "secret", raising=False)


def record_sends(monkeypatch):
    """Replace the sender with a recorder; returns the list of calls."""
    import app.utils.email_sender as email_sender

    calls: list[dict] = []

    async def fake_send(**kwargs):
        calls.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(email_sender, "send_email", fake_send)
    return calls


async def test_a_crash_mid_batch_keeps_the_records_of_sends_already_gone_out(monkeypatch):
    """A crash between two sends must not erase the record of the first one.

    This is the whole claim argument in one test. The old shape committed once
    at the end of the loop, so "the process died halfway" and "nothing was ever
    sent" were the same database -- and the next run re-sent every message that
    had already reached a customer.
    """
    smtp_on(monkeypatch)
    import app.utils.email_sender as email_sender

    calls: list[str] = []

    async def die_on_the_second(**kwargs):
        calls.append(kwargs["to_email"])
        if len(calls) >= 2:
            # BaseException, not Exception: the send branch catches Exception,
            # and a process dying is not an error the loop gets to narrate.
            raise KeyboardInterrupt("the process died between two sends")
        return {"success": True}

    monkeypatch.setattr(email_sender, "send_email", die_on_the_second)

    first = lead_for("CrashA")
    second = lead_for("CrashB")
    try:
        with pytest.raises(KeyboardInterrupt):
            await workflows.send_drafted_emails_workflow([first, second])

        # The first lead's send is fully settled: row exists and says sent.
        customer, logs = await rows_for(first["website"])
        assert customer is not None, "the customer written before the crash is on disk"
        assert len(logs) == 1
        assert logs[0].status == EmailStatus.SENT
        assert logs[0].sent_at is not None

        # The second lead's attempt was claimed and is still in flight -- which
        # is exactly what mark_stale_sends later resolves as "unknown". Better
        # a row that says "unknown" than no row that says "never happened".
        customer2, logs2 = await rows_for(second["website"])
        assert customer2 is not None
        assert len(logs2) == 1
        assert logs2[0].status == EmailStatus.SENDING
    finally:
        await drop_for(first["website"])
        await drop_for(second["website"])


async def test_a_send_to_an_existing_customer_still_writes_a_row(db, monkeypatch):
    """A mail to a customer already in the book must leave a trace too.

    The loop used to skip the log entirely when the website matched ("already
    exists"), so re-contacting a known customer produced a real send with no
    record: invisible to the trace, to reply tracking, and to anyone asking
    whether we had already written to these people.
    """
    from app.models.customer import Customer

    smtp_on(monkeypatch)
    record_sends(monkeypatch)

    lead = lead_for("Repeat")
    suffix = uuid.uuid4().hex[:8]
    existing = Customer(
        company_name=f"Repeat Co {suffix}",
        country="Germany",
        website=lead["website"],
        industry="textiles",
        stage="contacted",
        source="manual",
    )
    db.add(existing)
    await db.commit()

    try:
        result = await workflows.send_drafted_emails_workflow([lead])

        assert result["emails_sent"] == 1
        assert result["crm_saved"] == 0, "the customer is not new"
        assert result["send_results"][0]["crm"] == "already_exists"
        assert result["send_results"][0]["customer_id"] == existing.id

        customer, logs = await rows_for(lead["website"])
        assert len(logs) == 1, "the send is on the existing customer's record"
        assert logs[0].customer_id == existing.id
        assert logs[0].status == EmailStatus.SENT
    finally:
        await drop_for(lead["website"])


async def test_an_unrecordable_attempt_is_never_handed_to_smtp(monkeypatch):
    """If the claim cannot be written, the mail does not go out.

    The claim is what makes a later "did we send this?" answerable. Sending
    without one is how the same customer gets the same letter twice.
    """
    smtp_on(monkeypatch)
    calls = record_sends(monkeypatch)

    lead = lead_for("Unrecordable", relevance_score="not-a-number")

    try:
        result = await workflows.send_drafted_emails_workflow([lead])

        assert calls == [], "nothing was handed to the mail server"
        assert result["emails_sent"] == 0
        assert result["emails_failed"] == 1
        entry = result["send_results"][0]
        assert entry["status"] == "failed"
        assert "not sent" in entry["error"], "the message says what did not happen"
    finally:
        await drop_for(lead["website"])
