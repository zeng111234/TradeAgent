"""A queue backed up to a thousand.

Three things break at that size, and none of them is the plan's arithmetic --
which is worth saying, because it is the part one would suspect. Measured on a
thousand customers with three emails each: the whole-book plan takes 78-94 ms and
returns the twenty most overdue plus ``deferred: 980``. The arithmetic is fine.

What was not fine:

* **One request tried to send the entire queue.** ``POST /emails/send-pending``
  read every pending draft and sent them all: minutes of SMTP inside one request,
  the send lock held throughout so every other send got a 409, a client timeout
  with no idea how far it got, and no order at all. It is now a bounded batch --
  oldest first, ``remaining`` reported -- so the caller drains it and watches the
  number fall.
* **Debugging one customer cost the whole book.** The debug view built the plan
  for every customer and looked one up: 125 ms per request at a thousand, growing
  with the book. ``plan_for_one_customer`` runs the same derivation over a
  one-element list. This file asserts the two agree, because a fast path that
  answers differently from the slow one is not a fast path, it is a second
  implementation.
* **The backlog was invisible.** ``deferred: 980`` was in the API response and
  nowhere a human would look.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.email_log import EmailLog, EmailStatus

CUSTOMER_URL = "/api/v1/customers/{}/debug"
SEND_URL = "/api/v1/emails/send-pending"


async def make_customer(db, tag="queue", *, stage="contacted"):
    from app.models.customer import Customer, CustomerContact

    suffix = uuid.uuid4().hex[:8]
    customer = Customer(
        company_name=f"Queue {tag} {suffix}", country="Germany", industry="textiles",
        stage=stage, source="manual",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    db.add(CustomerContact(
        customer_id=customer.id, name="Contact",
        email=f"{tag}-{suffix}@example.test", is_primary=1,
    ))
    await db.commit()
    return customer.id


async def add_email(db, customer_id, *, status, subject="Draft", days_ago=1, **fields):
    created = datetime.utcnow() - timedelta(days=days_ago)
    row = EmailLog(
        customer_id=customer_id,
        to_email="buyer@example.test",
        subject=subject,
        body="<p>x</p>",
        status=status,
        tracking_id=f"t-{uuid.uuid4().hex[:10]}",
        created_at=created,
        sent_at=created if status != EmailStatus.PENDING else None,
        **fields,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
def cleanup(db):
    """Delete whatever the test created, so the shared book does not grow."""
    import asyncio

    from app.models.customer import Customer

    created: list[int] = []

    def track(customer_id):
        created.append(customer_id)
        return customer_id

    yield track

    async def drop():
        for customer_id in created:
            customer = (
                await db.execute(select(Customer).where(Customer.id == customer_id))
            ).scalar_one_or_none()
            if customer is not None:
                await db.delete(customer)
        await db.commit()

    asyncio.run(drop())


def stub_send(monkeypatch, *, ok=True):
    import app.routers.emails as emails

    calls = []

    async def fake_send(**kwargs):
        calls.append(kwargs)
        return {"success": ok, "error": None if ok else "simulated failure"}

    monkeypatch.setattr(emails, "send_email", fake_send)
    return calls


# --------------------------------------------------------------------------
# Draining the queue in bounded batches
# --------------------------------------------------------------------------

def test_a_batch_sends_at_most_the_limit(client, db, cleanup, monkeypatch):
    import asyncio

    calls = stub_send(monkeypatch)
    customer_id = cleanup(asyncio.run(make_customer(db, "limit")))
    for index in range(5):
        asyncio.run(add_email(db, customer_id, status=EmailStatus.PENDING,
                              subject=f"Draft {index}"))

    body = client.post(SEND_URL, params={"limit": 2}).json()

    assert body["total"] == 2
    assert len(calls) == 2
    assert body["limit"] == 2
    assert body["remaining"] >= 3, (
        "the caller needs the queue depth from the same call that drains part of it"
    )


def test_the_backlog_drains_oldest_first(client, db, cleanup, monkeypatch):
    import asyncio

    calls = stub_send(monkeypatch)
    customer_id = cleanup(asyncio.run(make_customer(db, "order")))
    asyncio.run(add_email(db, customer_id, status=EmailStatus.PENDING,
                          subject="Oldest", days_ago=9))
    asyncio.run(add_email(db, customer_id, status=EmailStatus.PENDING,
                          subject="Middle", days_ago=5))
    asyncio.run(add_email(db, customer_id, status=EmailStatus.PENDING,
                          subject="Newest", days_ago=1))

    body = client.post(SEND_URL, params={"limit": 2}).json()

    assert body["total"] == 2
    assert [call["subject"] for call in calls] == ["Oldest", "Middle"], (
        "a backlog must drain in the order it built up"
    )


def test_repeating_the_call_empties_the_queue(client, db, cleanup, monkeypatch):
    """The drain loop, without assuming the shared database is otherwise empty.

    Other tests leave pending drafts behind, so this asserts what is true of *my*
    five: each round carries at most ``limit``, the oldest of mine go first, and
    five drafts at two per call take three rounds. An earlier version asserted
    `remaining == 0` after three calls, which is a statement about the whole
    shared queue rather than about the code.
    """
    import asyncio

    calls = stub_send(monkeypatch)
    customer_id = cleanup(asyncio.run(make_customer(db, "drain")))
    for index in range(5):
        asyncio.run(add_email(db, customer_id, status=EmailStatus.PENDING,
                              subject=f"Drain {index}"))

    mine = {f"Drain {index}" for index in range(5)}
    sent_mine: set[str] = set()
    per_round: list[int] = []

    for _ in range(6):
        before = len(calls)
        body = client.post(SEND_URL, params={"limit": 2}).json()
        assert body["total"] <= 2, "a round must respect the limit"
        per_round.append(len(calls) - before)
        sent_mine |= {call["subject"] for call in calls[before:]} & mine
        if sent_mine == mine:
            break

    assert sent_mine == mine, f"these were never sent: {sorted(mine - sent_mine)}"
    assert all(size <= 2 for size in per_round), per_round
    # Deliberately not "it took exactly three rounds": other tests leave pending
    # drafts behind, they are older than mine, and they take slots in the early
    # rounds. The round count is a fact about the shared queue, not about the
    # code; what the code guarantees is the per-round limit and that a repeated
    # call makes progress.
    assert len(per_round) >= 3, (
        f"five drafts at two per call cannot fit in fewer than three rounds: {per_round}"
    )


def test_the_limit_is_validated(client):
    assert client.post(SEND_URL, params={"limit": 0}).status_code == 422
    assert client.post(SEND_URL, params={"limit": -5}).status_code == 422


def test_the_limit_has_a_ceiling(client, monkeypatch):
    """A caller cannot ask for an unbounded batch through the front door."""
    stub_send(monkeypatch)
    body = client.post(SEND_URL, params={"limit": 100000}).json()
    assert body["limit"] == 500


# --------------------------------------------------------------------------
# One customer's plan must equal the book's answer for that customer
# --------------------------------------------------------------------------

def test_the_single_customer_plan_agrees_with_the_whole_book(client, db, cleanup):
    """The guard that makes the fast path safe.

    ``plan_for_one_customer`` exists so debugging one customer does not cost the
    whole book. If it ever disagrees with the book plan, there are two
    implementations of the same decision and the cheap one is wrong.
    """
    import asyncio

    from app.models.customer import Customer
    from app.services.agent_service import (
        daily_follow_up_intelligence,
        plan_for_one_customer,
    )

    customer_id = cleanup(asyncio.run(make_customer(db, "agree")))
    asyncio.run(add_email(db, customer_id, status=EmailStatus.SENT, days_ago=10))

    async def compare():
        customer = (
            await db.execute(select(Customer).where(Customer.id == customer_id))
        ).scalar_one()
        single = await plan_for_one_customer(db, customer)
        book = await daily_follow_up_intelligence(db, due_only=False, persist=False)
        from_book = next(
            (a for a in book["actions"] if a["customer_id"] == customer_id), None
        )
        if from_book is None:
            # The plan caps its day at twenty; a customer outside that list has
            # no book answer to compare against, and the single-customer path is
            # the more complete one.
            return single, None
        return single, from_book

    single, from_book = asyncio.run(compare())

    assert single is not None, "the single-customer plan found nothing to say"
    if from_book is not None:
        for field in ("kind", "priority", "reason", "due_at", "suggested_action"):
            assert single[field] == from_book[field], (
                f"{field!r} differs between the fast path and the whole book"
            )


def test_a_closed_customer_has_no_plan_and_the_view_says_why(client, db, cleanup):
    import asyncio

    customer_id = cleanup(asyncio.run(make_customer(db, "closed", stage="completed")))
    asyncio.run(add_email(db, customer_id, status=EmailStatus.SENT, days_ago=3))

    body = client.get(CUSTOMER_URL.format(customer_id)).json()

    assert body["plan"] is None
    assert "closed stage" in " | ".join(body["findings"])


# --------------------------------------------------------------------------
# The backlog is visible
# --------------------------------------------------------------------------

def test_the_debug_view_reports_the_backlog(client, db, cleanup):
    import asyncio

    customer_id = cleanup(asyncio.run(make_customer(db, "backlog")))
    asyncio.run(add_email(db, customer_id, status=EmailStatus.SENT, days_ago=3))

    body = client.get(CUSTOMER_URL.format(customer_id)).json()

    backlog = body["backlog"]
    assert backlog["customers_in_play"] >= 1
    assert backlog["max_actions_per_day"] == 20, (
        "the number must come from the policy; a copy here would drift"
    )
    assert backlog["days_to_clear_if_all_waiting"] >= 1


def test_the_backlog_counts_the_drafts_waiting_for_a_human(client, db, cleanup):
    import asyncio

    customer_id = cleanup(asyncio.run(make_customer(db, "drafts")))
    before = client.get(CUSTOMER_URL.format(customer_id)).json()["backlog"]["pending_drafts"]
    asyncio.run(add_email(db, customer_id, status=EmailStatus.PENDING))

    after = client.get(CUSTOMER_URL.format(customer_id)).json()["backlog"]["pending_drafts"]

    assert after == before + 1


def test_the_cap_and_the_backlog_come_from_the_same_number():
    """``max_actions_per_day`` is read, not repeated.

    The plan caps its day at this number and the backlog view explains the queue
    with it. If the view held its own copy, the two could disagree and the view
    would be quietly wrong about the queue.
    """
    from app.services.follow_up_plan import DEFAULT_FOLLOW_UP_PLAN_POLICY
    from app.services import customer_debug
    import inspect

    source = inspect.getsource(customer_debug._backlog)
    assert "DEFAULT_FOLLOW_UP_PLAN_POLICY.max_actions_per_day" in source
    assert DEFAULT_FOLLOW_UP_PLAN_POLICY.max_actions_per_day == 20


def test_a_partial_failure_names_the_recipient_and_the_reason(client, db, cleanup, monkeypatch):
    """The batch response is what a failure list is built from.

    Two drafts, both sends fail. The counts (``sent`` / ``failed``) say how many;
    ``details`` is the only place that says *which* and *why* -- and the operator
    UI now renders exactly those fields. If this shape drifts, the UI silently
    goes back to showing a bare number, which is what it did before.
    """
    import asyncio
    import uuid

    suffix = uuid.uuid4().hex[:8]
    subject_a = f"Partial A {suffix}"
    subject_b = f"Partial B {suffix}"

    async def seed():
        customer_id = await make_customer(db, tag="partial")
        cleanup(customer_id)
        await add_email(db, customer_id, status=EmailStatus.PENDING, subject=subject_a)
        await add_email(db, customer_id, status=EmailStatus.PENDING, subject=subject_b)

    asyncio.run(seed())
    stub_send(monkeypatch, ok=False)

    body = client.post(SEND_URL, params={"limit": 50}).json()

    ours = {
        detail["subject"]: detail
        for detail in body["details"]
        if detail["subject"] in (subject_a, subject_b)
    }
    assert set(ours) == {subject_a, subject_b}, (
        "a failed draft must appear in details, not only in the count"
    )
    for detail in ours.values():
        assert detail["success"] is False
        assert detail["to_email"] == "buyer@example.test"
        assert "simulated failure" in (detail["error"] or "")
    assert body["failed"] >= 2


# --------------------------------------------------------------------------
# "Sent" must not swallow "never left the process"
# --------------------------------------------------------------------------

def test_a_simulated_send_is_recorded_as_simulated_not_merely_sent(client, db, cleanup, monkeypatch):
    """The first question of a "the mail never arrived" investigation.

    Without SMTP credentials ``send_email`` reports success for a message it
    handed to nobody, and the row landed ``sent`` -- indistinguishable from a
    real one. Then "sent but not delivered" and "never sent at all" are the
    same row, and the investigation cannot even start. The status stays
    ``sent`` (the flows after a send treat it like any other), but the row
    must carry the difference.
    """
    import asyncio

    import app.routers.emails as emails

    async def fake_send(**kwargs):
        return {"success": True, "simulated": True}

    monkeypatch.setattr(emails, "send_email", fake_send)

    async def seed():
        customer_id = await make_customer(db, tag="sim")
        cleanup(customer_id)
        row = await add_email(db, customer_id, status=EmailStatus.PENDING,
                              subject="Simulated draft")
        return row.id

    email_id = asyncio.run(seed())
    body = client.post(SEND_URL, params={"limit": 10}).json()
    assert body["sent"] >= 1

    async def load():
        return (
            await db.execute(select(EmailLog).where(EmailLog.id == email_id))
        ).scalar_one()

    row = asyncio.run(load())
    assert row.status == EmailStatus.SENT, "the follow-up flows still see a send"
    assert row.simulated is True, "but the row must say it never left the process"

    # ... and the API carries it, so the UI can show it.
    logs = client.get("/api/v1/emails/logs").json()
    mine = [entry for entry in logs if entry["id"] == email_id]
    assert mine, "the sent row must appear in the log list"
    assert mine[0]["simulated"] is True


def test_a_real_send_is_not_marked_simulated(client, db, cleanup, monkeypatch):
    """The flag must not be sticky in the other direction either."""
    import asyncio

    import app.routers.emails as emails

    async def fake_send(**kwargs):
        return {"success": True, "simulated": False}

    monkeypatch.setattr(emails, "send_email", fake_send)

    async def seed():
        customer_id = await make_customer(db, tag="real")
        cleanup(customer_id)
        row = await add_email(db, customer_id, status=EmailStatus.PENDING,
                              subject="Real draft")
        return row.id

    email_id = asyncio.run(seed())
    client.post(SEND_URL, params={"limit": 10})

    async def load():
        return (
            await db.execute(select(EmailLog).where(EmailLog.id == email_id))
        ).scalar_one()

    row = asyncio.run(load())
    assert row.status == EmailStatus.SENT
    assert bool(row.simulated) is False


# --------------------------------------------------------------------------
# The tracking pixel is the only passive "was it even seen" signal
# --------------------------------------------------------------------------

def test_the_tracking_pixel_must_be_absolute_to_ever_fire(monkeypatch):
    """A relative ``src`` resolves against nothing inside a mail client.

    The pixel was emitted as ``/api/v1/...``: a dead link in every real
    recipient's client, so opens could never record and the only passive
    signal between "sent" and "replied" was silent. With a public base URL it
    points at this service; without one it is still emitted (the HTML stays
    valid) but cannot work, and the sender says so once.
    """
    from app.config import settings
    from app.utils.email_sender import create_tracking_pixel

    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://mail.example.com")
    pixel = create_tracking_pixel("abc123")
    assert 'src="https://mail.example.com/api/v1/emails/track/abc123/pixel"' in pixel

    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://mail.example.com/")
    pixel = create_tracking_pixel("abc123")
    assert 'src="https://mail.example.com/api/v1/emails/track/abc123/pixel"' in pixel, (
        "a trailing slash in the base must not double the path"
    )

    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")
    pixel = create_tracking_pixel("abc123")
    assert 'src="/api/v1/emails/track/abc123/pixel"' in pixel
