"""Human in the loop: a proposal is written, and the state is suspended.

The only place this application suspends work is `EmailStatus.PENDING`: a draft
is written, and a person decides whether it goes out. Two things were wrong with
how that state was handled.

**It was invisible to the plan.** Measured, with one email sent ten days ago:

    with a pending draft   -> kind=send_follow_up  due=2026-10-09  reason="No answer to 1 email(s)"
    with no draft at all   -> kind=send_follow_up  due=2026-10-09  reason="No answer to 1 email(s)"

Byte-identical. So the daily list told a human to chase a customer whose email
was already written and sitting in their own approval queue.

**Rejection was a deletion.** `DELETE /logs/{id}` removed the row, so nothing
recorded that this approach had already been turned down, and the next run
proposed it again.

Every test here makes its own customer. Sharing one would let a draft from an
earlier test decide the answer for a later one, which is exactly the kind of
cross-contamination the assertions are about.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta

from app.models.customer import Customer
from app.models.email_log import EmailLog, EmailStatus
from app.models.task import Task  # noqa: F401  (registers the table)

_ids = itertools.count(1)


async def a_customer(db, *, stage="contacted"):
    customer = Customer(
        company_name=f"Approval Test {next(_ids)}",
        country="DE",
        stage=stage,
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


async def a_pending_draft(db, customer, *, days_old=0, subject="Proposed outreach"):
    row = EmailLog(
        customer_id=customer.id,
        to_email=f"buyer{customer.id}@example.com",
        to_name="Buyer",
        subject=subject,
        body="<p>proposed by the agent</p>",
        status=EmailStatus.PENDING,
        created_at=datetime.utcnow() - timedelta(days=days_old),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def a_sent_email(db, customer, *, days_ago=10):
    row = EmailLog(
        customer_id=customer.id,
        to_email=f"buyer{customer.id}@example.com",
        to_name="Buyer",
        subject="Earlier outreach",
        body="<p>x</p>",
        status=EmailStatus.SENT,
        sent_at=datetime.utcnow() - timedelta(days=days_ago),
        created_at=datetime.utcnow() - timedelta(days=days_ago),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def plan_entry(client, customer_id):
    plan = client.get("/api/v1/agent/follow-up-plan").json()
    return next((a for a in plan["actions"] if a["customer_id"] == customer_id), None)


def due_today(client, customer_id):
    plan = client.post("/api/v1/agent/daily-intelligence?due_only=true").json()
    return [a for a in plan["actions"] if a["customer_id"] == customer_id]


# --------------------------------------------------------------------------
# Suspended, not due
# --------------------------------------------------------------------------

async def test_a_waiting_proposal_suspends_the_customer(db, client):
    customer = await a_customer(db)
    await a_pending_draft(db, customer)

    entry = plan_entry(client, customer.id)
    assert entry is not None
    assert entry["kind"] == "awaiting_approval"
    assert "waiting for your approval" in entry["reason"]
    assert entry["due_in_days"] > 0, "suspended: not due while it is fresh"
    assert "draft_email" not in entry, "you cannot propose a proposal"

    # And the scheduled job has nothing to do for this customer today.
    assert due_today(client, customer.id) == []


async def test_a_proposal_left_for_days_is_raised_as_a_reminder(db, client):
    customer = await a_customer(db)
    await a_pending_draft(db, customer, days_old=5)

    entries = due_today(client, customer.id)
    assert len(entries) == 1
    assert entries[0]["kind"] == "awaiting_approval"
    assert entries[0]["priority"] == "high"
    assert "oldest 5 day(s)" in entries[0]["reason"]


async def test_the_plan_stops_proposing_while_a_proposal_is_pending(db, client):
    """The defect: the entry was identical to having no draft at all."""
    with_proposal = await a_customer(db)
    await a_sent_email(db, with_proposal, days_ago=10)
    await a_pending_draft(db, with_proposal)

    without = await a_customer(db)
    await a_sent_email(db, without, days_ago=10)

    suspended = plan_entry(client, with_proposal.id)
    chased = plan_entry(client, without.id)

    assert chased["kind"] == "send_follow_up"
    assert suspended["kind"] == "awaiting_approval"
    assert suspended["kind"] != chased["kind"], (
        "a proposal waiting for approval must not read as work still to do"
    )


async def test_sending_the_draft_ends_the_suspension(db, client, monkeypatch):
    """The gate closes when the human approves."""
    import app.routers.emails as emails_router

    customer = await a_customer(db)
    draft = await a_pending_draft(db, customer)

    async def sender(to_email, to_name, subject, body, tracking_id=None, sender=None):
        return {"success": True, "simulated": True}

    monkeypatch.setattr(emails_router, "send_email", sender)
    assert client.post(f"/api/v1/emails/send-pending/{draft.id}").status_code == 200

    entry = plan_entry(client, customer.id)
    assert entry["kind"] != "awaiting_approval", entry
    assert entry["evidence"]["pending_proposals"] == 0


# --------------------------------------------------------------------------
# Rejecting is a decision, and it is kept
# --------------------------------------------------------------------------

async def test_rejecting_a_proposal_records_it_instead_of_deleting_it(db, client):
    customer = await a_customer(db)
    draft = await a_pending_draft(db, customer)

    response = client.post(f"/api/v1/emails/logs/{draft.id}/reject",
                           json={"reason": "wrong angle for this buyer"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rejected"] is True
    assert body["already_rejected"] is False
    assert body["conversation"]["pending_proposals"] == 0
    assert body["conversation"]["rejected_proposals"] == 1

    # The row is still there -- a refusal is information.
    logs = {row["id"]: row for row in client.get("/api/v1/emails/logs").json()}
    assert draft.id in logs, "a rejected proposal must not be deleted"
    assert logs[draft.id]["status"] == "rejected"


async def test_rejecting_twice_does_not_change_the_first_decision(db, client):
    customer = await a_customer(db)
    draft = await a_pending_draft(db, customer)

    first = client.post(f"/api/v1/emails/logs/{draft.id}/reject", json={"reason": "first"}).json()
    second = client.post(f"/api/v1/emails/logs/{draft.id}/reject", json={"reason": "second"}).json()

    assert first["rejected"] is True
    assert second["rejected"] is False
    assert second["already_rejected"] is True
    assert second["reason"] == "first", "the first decision stands"


async def test_a_rejected_draft_is_never_sent(monkeypatch, db, client):
    """`send-pending` sends what is *pending*. A rejected row is not."""
    import app.routers.emails as emails_router

    customer = await a_customer(db)
    draft = await a_pending_draft(db, customer)
    client.post(f"/api/v1/emails/logs/{draft.id}/reject")

    sent: list[str] = []

    async def sender(to_email, to_name, subject, body, tracking_id=None, sender=None):
        sent.append(to_email)
        return {"success": True, "simulated": True}

    monkeypatch.setattr(emails_router, "send_email", sender)
    client.post("/api/v1/emails/send-pending")

    assert draft.to_email not in sent, f"a rejected draft was sent: {sent}"
    logs = {row["id"]: row for row in client.get("/api/v1/emails/logs").json()}
    assert logs[draft.id]["status"] == "rejected"


async def test_a_draft_that_was_already_sent_cannot_be_rejected(db, client):
    customer = await a_customer(db)
    sent = await a_sent_email(db, customer, days_ago=0)

    response = client.post(f"/api/v1/emails/logs/{sent.id}/reject")
    assert response.status_code == 409
    assert "waiting for approval" in response.json()["detail"]


def test_rejecting_an_unknown_draft_is_a_404(client):
    assert client.post("/api/v1/emails/logs/999999/reject").status_code == 404


# --------------------------------------------------------------------------
# After a refusal the plan still speaks, and it says what happened
# --------------------------------------------------------------------------

async def test_after_a_refusal_the_customer_is_planned_for_again_with_the_reason(db, client):
    customer = await a_customer(db)
    draft = await a_pending_draft(db, customer)
    client.post(f"/api/v1/emails/logs/{draft.id}/reject")

    entry = plan_entry(client, customer.id)
    assert entry is not None, "a refused proposal must not drop the customer"
    assert entry["kind"] == "send_follow_up"
    assert entry["evidence"]["rejected_proposals"] == 1
    assert "rejected" in entry["reason"]


async def test_the_agent_can_see_that_a_proposal_is_waiting(db, client):
    """The agent's own view of the customer has to carry the suspension too."""
    from app.agent.trade_tools import get_customer_signals

    customer = await a_customer(db)
    await a_pending_draft(db, customer)

    result = await get_customer_signals(customer_id=customer.id)

    assert result["conversation"]["pending_proposals"] == 1
    assert result["conversation"]["pending_proposal_days"] == 0
    assert result["conversation"]["rejected_proposals"] == 0


# --------------------------------------------------------------------------
# The human's other two hands: edit and delete. Both are for proposals only.
# --------------------------------------------------------------------------
# The endpoints said "update a pending draft" and "remove a pending draft"
# and enforced neither -- so a sent row's subject and body could be rewritten
# and the row deleted, which destroys the record of what a customer actually
# received (the evidence a "the mail never arrived" investigation reads) and
# the record of a decision already made. The reject endpoint had the guard;
# these two did not.


async def reloaded(email_id):
    """Read the row through a *fresh* session.

    The shared test session keeps copies in its identity map
    (``expire_on_commit=False``), so a SELECT after an API request can return
    what the row used to be. A new session reads what the database now says.
    """
    from sqlalchemy import select

    from app.database import async_session

    async with async_session() as fresh:
        return (
            await fresh.execute(select(EmailLog).where(EmailLog.id == email_id))
        ).scalar_one_or_none()


# These four customers are created at a stage the plan does not cover
# (PLANNED_STAGES is contacted/interested/quoting) on purpose: the plan is
# a top-N list over the whole book, so a planned-stage customer left behind
# by one test can push another test's customer out of the list. The guards
# under test do not care about stage.

async def test_a_sent_row_cannot_be_edited(db, client):
    customer = await a_customer(db, stage="completed")
    draft = await a_pending_draft(db, customer)
    draft.status = EmailStatus.SENT
    draft.sent_at = datetime.utcnow()
    draft.subject = "What the customer actually received"
    await db.commit()

    r = client.put(f"/api/v1/emails/logs/{draft.id}", json={"subject": "Rewritten later"})

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "nothing was changed" in detail, "the reader must know the write did not happen"
    assert "sent" in detail

    row = await reloaded(draft.id)
    assert row.subject == "What the customer actually received"


async def test_a_sent_row_cannot_be_deleted(db, client):
    customer = await a_customer(db, stage="completed")
    draft = await a_pending_draft(db, customer)
    draft.status = EmailStatus.SENT
    draft.sent_at = datetime.utcnow()
    await db.commit()

    r = client.delete(f"/api/v1/emails/logs/{draft.id}")

    assert r.status_code == 409
    assert "nothing was deleted" in r.json()["detail"]

    assert await reloaded(draft.id) is not None, (
        "a sent row is the evidence of what went out"
    )


async def test_a_rejected_decision_is_not_deletable_either(db, client):
    customer = await a_customer(db, stage="completed")
    draft = await a_pending_draft(db, customer)
    client.post(f"/api/v1/emails/logs/{draft.id}/reject")

    r = client.delete(f"/api/v1/emails/logs/{draft.id}")
    assert r.status_code == 409, (
        "a rejection is a decision the next proposal must not repeat; "
        "deleting it would make the next run propose the same thing again"
    )

    row = await reloaded(draft.id)
    assert row.status == EmailStatus.REJECTED


async def test_a_pending_draft_is_still_editable_and_deletable(db, client):
    """The guards must not close the door they were meant to protect."""
    customer = await a_customer(db, stage="completed")
    draft = await a_pending_draft(db, customer)

    r = client.put(
        f"/api/v1/emails/logs/{draft.id}",
        json={"subject": "Edited before sending"},
    )
    assert r.status_code == 200

    row = await reloaded(draft.id)
    assert row.subject == "Edited before sending"

    r = client.delete(f"/api/v1/emails/logs/{draft.id}")
    assert r.status_code == 204

    assert await reloaded(draft.id) is None
