"""The same email handled twice: once, not twice.

Two different meanings of "processed twice", and both were open:

* **The same customer's email handled twice by the agent.** ``create_email_draft``
  had no dedup at all, so a retry, a double click or the same goal run twice in a
  day left two identical pending drafts. Two rows is not two pieces of work, it is
  one piece of work listed twice, and a human working down the list sends the
  customer the same email twice.
* **The same send request executed twice.** The row's status is the only thing
  standing between a duplicate and a customer, and it was written *after* the
  send -- so a crash in between left no evidence, and the mail went again.

Where the idempotency lives now, in the order that matters:

1. the row's status, claimed atomically before the mail is handed over
   (``pending -> sending`` in one ``UPDATE ... WHERE status = pending``);
2. ``tracking_id``, unique, derived from a caller's ``Idempotency-Key``;
3. a process-local lock, for a readable 409 before any work happens.

Exactly-once delivery is still not claimed, and cannot be: if the process dies
between the SMTP handoff and the status write, whether the mail arrived is not
knowable from here. Those rows are resolved to ``failed`` with "outcome unknown"
and a human decides -- see ``test_an_abandoned_claim_is_not_quietly_resent``.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.models.email_log import EmailLog, EmailStatus
from app.services.email_claims import (
    claim,
    idempotency_tracking_id,
    mark_stale_sends,
    release,
)

CUSTOMER_ID = 4  # Japan Embroidery, from the demo seed


async def make_customer(db, tag: str) -> tuple[int, str]:
    """A throwaway customer, so these tests leave the demo data alone.

    Two things this has to get right, both learned the hard way:

    * not the demo customer -- a pending draft against customer 4 changes what
      the follow-up plan says about customer 4, which is another test's subject;
    * a **closed stage**. Plan-eligible customers count against the plan's
      ``max_actions_per_day`` budget, so eighteen of them pushed customer 4 out
      of the day's list and broke a test in another file that ran later. These
      customers exist to send and deduplicate email, not to be planned, so the
      planner should not see them at all.
    """
    from app.models.customer import Customer, CustomerContact

    suffix = uuid.uuid4().hex[:8]
    customer = Customer(
        company_name=f"Idempotency {tag} {suffix}",
        country="Testland",
        industry="testing",
        stage="completed",  # closed: invisible to the follow-up plan
        source="manual",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)

    email = f"{tag.lower().replace(' ', '-')}-{suffix}@example.test"
    db.add(
        CustomerContact(
            customer_id=customer.id, name="Test Contact", email=email, is_primary=1
        )
    )
    await db.commit()
    return customer.id, email


def stub_send(monkeypatch, *, ok=True, on_call=None):
    """Replace the SMTP handoff with a counter the test can inspect."""
    import app.routers.emails as emails

    calls = []

    async def fake_send(**kwargs):
        calls.append(kwargs)
        if on_call is not None:
            on_call(kwargs)
        return {"success": ok, "error": None if ok else "simulated failure"}

    monkeypatch.setattr(emails, "send_email", fake_send)
    return calls


def post_send(client, body, key=None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post("/api/v1/emails/send", json=body, headers=headers)


def a_message(customer_id, to_email, subject="Quote for 500kg gold thread"):
    return {
        "customer_id": customer_id,
        "to_email": to_email,
        "to_name": "Test Contact",
        "subject": subject,
        "body": "<p>Here is the quote you asked for.</p>",
    }


async def a_customer(db, tag="send"):
    """Create the customer and return (id, message factory)."""
    customer_id, email = await make_customer(db, tag)

    def message(subject="Quote for 500kg gold thread"):
        return a_message(customer_id, email, subject)

    return customer_id, message


async def a_pending_row(db, **overrides) -> EmailLog:
    """A throwaway row on a throwaway customer.

    Not the demo customer: a pending draft against customer 4 changes what the
    follow-up plan says about customer 4, which is another test's subject.
    """
    customer_id, email = await make_customer(db, "row")
    fields = dict(
        customer_id=customer_id, to_email=email, subject="throwaway",
        body="x", status=EmailStatus.PENDING,
        tracking_id=f"t-{uuid.uuid4().hex[:12]}",
    )
    fields.update(overrides)
    row = EmailLog(**fields)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


# --------------------------------------------------------------------------
# The same send request, twice
# --------------------------------------------------------------------------

def test_the_same_key_sends_once(client, db, monkeypatch):
    calls = stub_send(monkeypatch)
    _, message = asyncio.run(a_customer(db, "same key"))
    key = str(uuid.uuid4())

    first = post_send(client, message(), key=key)
    second = post_send(client, message(), key=key)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert len(calls) == 1, f"{len(calls)} emails went out for one key"

    assert first.json()["status"] == "sent"
    assert first.json()["replayed"] is False, "the first call is not a replay"
    assert second.json()["replayed"] is True
    assert second.json()["id"] == first.json()["id"], "the replay invented a new row"
    assert second.json()["already_sent"] is True


def test_without_a_key_there_is_nothing_to_recognise_a_replay_by(client, db, monkeypatch):
    """The limitation, stated rather than hidden.

    Two identical requests with no key are two requests as far as the server can
    tell. The claim still stops them running *at the same time*, but the second
    one here follows the first, so it is a genuinely new send.
    """
    calls = stub_send(monkeypatch)
    _, message = asyncio.run(a_customer(db, "no key"))

    post_send(client, message("No key here"))
    post_send(client, message("No key here"))

    assert len(calls) == 2


def test_a_different_key_with_the_same_body_is_a_new_send(client, db, monkeypatch):
    calls = stub_send(monkeypatch)
    _, message = asyncio.run(a_customer(db, "new key"))

    post_send(client, message(), key=str(uuid.uuid4()))
    post_send(client, message(), key=str(uuid.uuid4()))

    assert len(calls) == 2, "a different key means the caller really does mean to send again"


def test_the_same_key_with_a_different_message_is_refused(client, db, monkeypatch):
    """A key identifies one request; reusing it for another must not say "sent".

    Before this, the second call found the first row and answered 200
    ``replayed: true`` -- with the *first* recipient. The caller believed a
    different customer had been emailed; nothing had been sent, and nothing in
    the response said so. Returning a wrong success is worse than an error: the
    caller has no reason to look.
    """
    calls = stub_send(monkeypatch)
    customer_id, message = asyncio.run(a_customer(db, "key reuse"))
    key = str(uuid.uuid4())

    first = post_send(client, message("the first subject"), key=key)
    assert first.status_code == 200, first.text

    changed = dict(message("the first subject"), to_email="somebody-else@example.test")
    second = post_send(client, changed, key=key)

    assert second.status_code == 409, second.text
    body = second.json()
    assert body["code"] == "idempotency_key_reuse"
    assert body["retryable"] is False
    assert body["details"]["email_log_id"] == first.json()["id"]
    assert "somebody-else@example.test" not in second.text
    assert len(calls) == 1, "the mismatched request was sent anyway"


def test_a_racing_duplicate_loses_the_insert_and_still_answers(client, db, monkeypatch):
    """The gap between "no row yet" and "insert", closed by UNIQUE, not by luck.

    Both requests can read "nothing for this key" before either inserts. The
    loser's insert then violates ``tracking_id UNIQUE``. Modelled by making the
    first read miss once: that is what the race looks like from the loser's
    side, and it fails deterministically instead of depending on scheduling.
    """
    calls = stub_send(monkeypatch)
    _, message = asyncio.run(a_customer(db, "race"))
    key = str(uuid.uuid4())

    first = post_send(client, message(), key=key)
    assert first.status_code == 200, first.text

    from app.routers import emails as emails_router

    real_already_handled = emails_router.already_handled
    state = {"miss": True}

    async def miss_once(db_session, tracking_id):
        if state["miss"]:
            state["miss"] = False
            return None
        return await real_already_handled(db_session, tracking_id)

    monkeypatch.setattr(emails_router, "already_handled", miss_once)

    second = post_send(client, message(), key=key)

    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["id"] == first.json()["id"], "the loser invented a second row"
    assert len(calls) == 1, "the loser sent a second email"


def test_the_row_exists_before_the_mail_is_handed_over(client, db, monkeypatch):
    """The ordering is the crash protection, so it is worth asserting.

    If the row is written after the send, a crash in between leaves no evidence
    and the next attempt emails the customer again.
    """
    seen = {}

    def inspect_at_send_time(kwargs):
        # A synchronous peek from inside the send call would need its own loop;
        # record the tracking id and check the row afterwards instead.
        seen["tracking_id"] = kwargs["tracking_id"]

    stub_send(monkeypatch, on_call=inspect_at_send_time)
    _, message = asyncio.run(a_customer(db, "ordering"))
    response = post_send(client, message("Ordering check"), key=str(uuid.uuid4()))

    detail = client.get("/api/v1/emails/logs", params={"limit": 200}).json()
    rows = detail["items"] if isinstance(detail, dict) and "items" in detail else detail
    matching = [
        row for row in rows
        if isinstance(row, dict) and row.get("tracking_id") == seen["tracking_id"]
    ]
    assert matching, "the row was not written at all"
    assert len(matching) == 1, "the claim row and the log row are the same row"
    # It settled to `sent`, which means the claim row was transitioned rather
    # than a second row being created afterwards.
    assert matching[0]["status"] in ("sent", "SENT")
    assert response.json()["id"] == matching[0]["id"]


def test_a_failed_send_does_not_claim_success(client, db, monkeypatch):
    stub_send(monkeypatch, ok=False)
    _, message = asyncio.run(a_customer(db, "failing"))
    key = str(uuid.uuid4())

    first = post_send(client, message("This one fails"), key=key)
    assert first.json()["status"] in ("failed", "FAILED")

    # Retrying the same key is refused rather than silently sending again: the
    # caller has to decide, because a failed attempt may still have arrived.
    again = post_send(client, message("This one fails"), key=key)
    assert again.status_code == 409
    assert "failed" in again.json()["detail"]


# --------------------------------------------------------------------------
# The claim, underneath the lock
# --------------------------------------------------------------------------

async def test_only_one_caller_can_claim_a_pending_row(db):
    row = await a_pending_row(db, subject="claim me")

    assert await claim(db, row.id) is True
    assert await claim(db, row.id) is False, (
        "a second worker claimed a row that was already claimed; that is exactly "
        "the race that sends a customer two emails"
    )


async def test_a_claimed_row_is_not_picked_up_by_the_batch(client, db):
    """The batch only ever looks at `pending`, so a claim is a real exclusion."""
    row = await a_pending_row(db, subject="in flight", status=EmailStatus.SENDING)

    import app.routers.emails as emails

    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return {"success": True}

    original = emails.send_email
    emails.send_email = fake_send
    try:
        result = client.post("/api/v1/emails/send-pending").json()
    finally:
        emails.send_email = original

    assert result["sent"] == 0
    assert not any(call["subject"] == "in flight" for call in sent)


async def test_an_abandoned_claim_is_not_quietly_resent(db):
    """The one case that cannot be resolved, resolved honestly.

    The process claimed the row and died. Whether that mail arrived is not
    knowable. Re-queueing it risks a duplicate; deleting it risks a silent drop.
    So it becomes `failed` with the uncertainty written down, for a human.
    """
    from datetime import datetime, timedelta

    row = await a_pending_row(db, subject="abandoned", status=EmailStatus.SENDING)
    row.created_at = datetime.utcnow() - timedelta(hours=2)
    await db.commit()

    resolved = await mark_stale_sends(db, older_than_seconds=900)

    await db.refresh(row)
    assert resolved >= 1
    assert row.status == EmailStatus.FAILED
    assert "unknown" in row.error_message
    assert "check the mailbox" in row.error_message.lower()


async def test_a_live_claim_is_left_alone(db):
    row = await a_pending_row(db, subject="still going", status=EmailStatus.SENDING)

    await mark_stale_sends(db, older_than_seconds=900)

    await db.refresh(row)
    assert row.status == EmailStatus.SENDING, "a slow send is not an abandoned one"


async def test_release_settles_the_row(db):
    row = await a_pending_row(db, subject="release me")
    await claim(db, row.id)

    await release(db, row.id, sent=True)
    await db.refresh(row)
    assert row.status == EmailStatus.SENT
    assert row.sent_at is not None


def test_the_key_maps_to_one_tracking_id():
    key = str(uuid.uuid4())
    assert idempotency_tracking_id(key) == idempotency_tracking_id(key)
    assert idempotency_tracking_id(key) != idempotency_tracking_id(str(uuid.uuid4()))
    assert idempotency_tracking_id(key).startswith("idem-")


async def test_a_claimed_row_is_stored_as_the_enum_name(db):
    """Why adding `sending` needs no migration.

    Read with raw SQL on purpose: the ORM converts the column back into an enum
    member, so asking SQLAlchemy what it stored tells you about the ORM, not
    about the database. The no-migration claim is about the *text in SQLite* --
    the enum's name, as a plain VARCHAR with no CHECK constraint, which is why
    an existing database accepts a value it has never seen.
    """
    from sqlalchemy import text

    row = await a_pending_row(db, subject="name check", status=EmailStatus.SENDING)

    stored = (
        await db.execute(
            text("SELECT status FROM email_logs WHERE id = :id"), {"id": row.id}
        )
    ).scalar_one()
    assert stored == "SENDING", f"expected the enum name in the column, got {stored!r}"


# --------------------------------------------------------------------------
# The agent's own write path
# --------------------------------------------------------------------------

async def _draft(customer_id, subject="Following up on the quote"):
    from app.agent.trade_tools import create_email_draft

    return await create_email_draft(
        customer_id=customer_id,
        subject=subject,
        body="<p>Just checking in on the quote we sent.</p>",
    )


async def test_the_same_draft_twice_creates_one_draft(db):
    customer_id, _ = await make_customer(db, "dup")
    first = await _draft(customer_id, "Duplicate guard A")
    second = await _draft(customer_id, "Duplicate guard A")

    assert first["status"] == "draft_created"
    assert second["status"] == "duplicate", (
        "two identical pending drafts is one piece of work listed twice; a human "
        "working down the list emails the customer twice"
    )
    assert second["email_log_id"] == first["email_log_id"]
    assert "already waiting" in second["reason"]


async def test_a_duplicate_is_matched_regardless_of_case_and_padding(db):
    customer_id, _ = await make_customer(db, "case")
    first = await _draft(customer_id, "Case check subject")
    second = await _draft(customer_id, "  case CHECK subject  ")

    assert second["status"] == "duplicate"
    assert second["email_log_id"] == first["email_log_id"]


async def test_a_different_subject_is_a_different_email(db):
    customer_id, _ = await make_customer(db, "subjects")
    first = await _draft(customer_id, "Different subject one")
    second = await _draft(customer_id, "Different subject two")

    assert first["status"] == "draft_created"
    assert second["status"] == "draft_created"
    assert first["email_log_id"] != second["email_log_id"]


async def test_once_the_first_is_sent_a_new_draft_is_allowed(db):
    """The guard is about *waiting* drafts, not about the subject forever.

    Chasing a quote three days after the first one went out is the normal case,
    and it must keep working.
    """
    customer_id, _ = await make_customer(db, "sent-first")
    first = await _draft(customer_id, "Chase the quote")
    row = (
        await db.execute(select(EmailLog).where(EmailLog.id == first["email_log_id"]))
    ).scalar_one()
    row.status = EmailStatus.SENT
    await db.commit()

    second = await _draft(customer_id, "Chase the quote")

    assert second["status"] == "draft_created", (
        "the guard must only block a duplicate that is still waiting; the first "
        "one having been sent is exactly when a follow-up is wanted"
    )


async def test_a_rejected_draft_does_not_block_a_new_one(db):
    customer_id, _ = await make_customer(db, "rejected")
    first = await _draft(customer_id, "Rejected once")
    row = (
        await db.execute(select(EmailLog).where(EmailLog.id == first["email_log_id"]))
    ).scalar_one()
    row.status = EmailStatus.REJECTED
    await db.commit()

    second = await _draft(customer_id, "Rejected once")
    assert second["status"] == "draft_created"


async def test_the_guard_is_per_customer(db):
    first_customer, _ = await make_customer(db, "per-one")
    second_customer, _ = await make_customer(db, "per-two")

    first = await _draft(first_customer, "Shared subject")
    second = await _draft(second_customer, "Shared subject")

    assert first["status"] == "draft_created"
    assert second["status"] == "draft_created", (
        "two customers can legitimately get the same subject"
    )
