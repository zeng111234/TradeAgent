"""One hundred customers followed at once: what keeps them apart.

A hundred customers is not a scale problem in this application -- it is a
one-process, one-event-loop, one-SQLite-file program, and the plan for a hundred
customers is the same arithmetic as the plan for three. What breaks first is not
throughput, it is **overlap**: two things touching the same rows at the same
time. Measured before any of this was guarded, ten pending drafts and two
concurrent batch sends produced **twenty SMTP calls**, every customer receiving
the same email twice, while both calls reported success and every row ended up
``sent`` -- so the duplication left no trace in the record at all.

So there are two separate claims in this file, and they need different methods:

* **isolation** -- customer A's history must not reach customer B's plan. That is
  a property of the derivation, and it is tested at a hundred customers.
* **overlap** -- the same rows must not be claimed twice. That is a property of
  the locks, and it is tested by actually running things concurrently.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import event

from app.main import app
from app.services.agent_service import _PLAN_LOCK, daily_follow_up_intelligence
from app.services.follow_up_plan import (
    DEFAULT_FOLLOW_UP_PLAN_POLICY,
    CustomerPlanInput,
    plan_follow_ups,
)
from app.services.follow_up_policy import derive_thread_state

NOW = datetime(2026, 10, 16, 12, 0, 0)


def row(*, sent_days_ago=None, replied_days_ago=None, status=None):
    sent_at = NOW - timedelta(days=sent_days_ago) if sent_days_ago is not None else None
    replied_at = (
        NOW - timedelta(days=replied_days_ago) if replied_days_ago is not None else None
    )
    return type("Row", (), {
        "status": status or ("replied" if replied_at is not None else "sent"),
        "created_at": sent_at,
        "sent_at": sent_at,
        "replied_at": replied_at,
    })()


# --------------------------------------------------------------------------
# Isolation: a hundred customers, a hundred distinct histories
# --------------------------------------------------------------------------

def a_hundred_customers():
    """Each customer i has a history no other customer has.

    The histories differ in *length* and in *whether they replied*, so any
    positional mix-up -- pairing one customer's rows with another's, or reusing a
    loop variable -- changes the answer for at least one of them.
    """
    states = []
    for i in range(100):
        kind = i % 5
        if kind == 0:
            emails = []                                    # nothing on record
        elif kind == 1:
            emails = [row(sent_days_ago=10)]               # one, unanswered
        elif kind == 2:
            emails = [row(sent_days_ago=10), row(sent_days_ago=4)]   # two
        elif kind == 3:
            emails = [row(sent_days_ago=12, replied_days_ago=3)]     # answered
        else:
            emails = [row(sent_days_ago=d) for d in (40, 34, 28, 22)]  # capped
        states.append(CustomerPlanInput(
            customer_id=1000 + i,
            company_name=f"Customer {1000 + i}",
            stage="contacted",
            thread=derive_thread_state(list(emails), NOW),
            has_any_email=bool(emails),
            contact_email=f"buyer{i}@example.com",
        ))
    return states


def test_every_customer_is_judged_only_on_their_own_history():
    states = a_hundred_customers()
    plan = plan_follow_ups(states, now=NOW)

    by_id = {a.customer_id: a for a in plan.commitments}
    assert len(by_id) == 100, "one commitment per customer, no duplicates"

    for i, state in enumerate(states):
        action = by_id[state.customer_id]
        expected = state.thread.awaiting_reply
        assert action.attempt == expected, (
            f"customer {state.customer_id} was judged on someone else's history: "
            f"attempt {action.attempt}, own awaiting_reply {expected}"
        )
        assert action.contact_email == state.contact_email, (
            "a contact address must never come from another customer's row"
        )
        assert action.evidence["awaiting_reply"] == expected
        assert action.evidence["answered"] == state.thread.answered


def test_the_kind_follows_from_the_customer_s_own_history():
    states = a_hundred_customers()
    plan = plan_follow_ups(states, now=NOW)
    by_id = {a.customer_id: a for a in plan.commitments}

    for i in range(100):
        action = by_id[1000 + i]
        kind = i % 5
        if kind == 4:
            assert action.kind in ("change_channel", "review"), action.kind
        elif kind == 3:
            assert action.kind == "answer_them", action.kind
        else:
            assert action.kind == "send_follow_up", action.kind


def test_a_hundred_customers_is_still_one_bounded_day():
    plan = plan_follow_ups(a_hundred_customers(), now=NOW, due_only=True)

    assert len(plan.actions) <= DEFAULT_FOLLOW_UP_PLAN_POLICY.max_actions_per_day
    assert plan.deferred == len([a for a in plan.commitments if a.due_at <= NOW]) \
        - len(plan.actions)
    # The cap is on today's list, not on the commitments: nobody is forgotten.
    assert len(plan.commitments) == 100


def test_the_order_does_not_depend_on_which_customer_was_read_first():
    states = a_hundred_customers()
    forwards = plan_follow_ups(states, now=NOW).as_dict()
    backwards = plan_follow_ups(list(reversed(states)), now=NOW).as_dict()

    assert forwards == backwards, (
        "the plan is a function of the state, not of the order the rows arrived in"
    )


# --------------------------------------------------------------------------
# Isolation across the database boundary
# --------------------------------------------------------------------------

async def test_the_batched_read_is_keyed_by_customer_not_by_position(db):
    """Three queries for the book, and each customer gets their own rows."""
    from sqlalchemy import select as sa_select

    from app.models.customer import Customer
    from app.models.email_log import EmailLog, EmailStatus

    created = []
    for index, count in enumerate((1, 2, 3)):
        customer = Customer(
            company_name=f"Batch Test {index}", country="DE", stage="contacted"
        )
        db.add(customer)
        await db.flush()
        for n in range(count):
            db.add(EmailLog(
                customer_id=customer.id, to_email=f"batch{index}@example.com",
                subject=f"email {n}", body="<p>x</p>",
                status=EmailStatus.SENT,
                sent_at=datetime.utcnow() - timedelta(days=10 - n),
                created_at=datetime.utcnow() - timedelta(days=10 - n),
            ))
        created.append((customer.id, count))
    await db.commit()

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.bind.sync_engine, "before_cursor_execute", record)
    try:
        plan = await daily_follow_up_intelligence(db_session=db, persist=False)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", record)

    email_reads = [s for s in statements if "FROM email_logs" in s]
    contact_reads = [s for s in statements if "FROM customer_contacts" in s]
    assert len(email_reads) == 1, f"one query for every customer's emails: {email_reads}"
    assert len(contact_reads) == 1, f"one query for every customer's contacts: {contact_reads}"

    by_id = {a["customer_id"]: a for a in plan["actions"]}
    for customer_id, count in created:
        entry = by_id.get(customer_id)
        assert entry is not None, "a customer with an unanswered email must be planned"
        assert entry["attempt"] == count, (
            f"customer {customer_id} has {count} emails but the plan counted "
            f"{entry['attempt']}"
        )


async def test_a_read_only_plan_does_not_take_the_lock(db):
    """Blocking a dashboard behind a slow write is how a dashboard starts timing out."""
    assert _PLAN_LOCK.acquire(blocking=False)
    try:
        plan = await daily_follow_up_intelligence(db_session=db, persist=False)
        assert "skipped" not in plan
        assert isinstance(plan["actions"], list)
    finally:
        _PLAN_LOCK.release()


async def test_a_second_committing_plan_run_is_skipped_not_doubled(db):
    """The cron job and a human clicking the button both call this."""
    assert _PLAN_LOCK.acquire(blocking=False)
    try:
        plan = await daily_follow_up_intelligence(db_session=db, persist=True)
    finally:
        _PLAN_LOCK.release()

    assert plan["skipped"] is True
    assert plan["actions"] == []
    # It still has to answer with the shape its callers read.
    for key in ("total_actions", "deferred", "by_kind", "actions", "commitments"):
        assert key in plan


# --------------------------------------------------------------------------
# Overlap: the same drafts must not be sent twice
# --------------------------------------------------------------------------

async def test_two_concurrent_batch_sends_do_not_email_anyone_twice(monkeypatch):
    """The defect: two calls, the same pending drafts, both sent.

    What is asserted is the invariant -- never more than one send per draft, and
    exactly one caller refused -- not a particular interleaving. The duplication
    itself was *observed* under this shape (ten drafts, twenty SMTP calls) and
    does not reproduce on demand, because whether the second call's read lands
    inside the first call's send window depends on scheduling. The mechanism
    does not: read and write are separated by the whole SMTP round trip with
    nothing claimed between them. So the gate below holds the first call inside
    that window, and the second call is started there; whether it duplicates or
    is refused is then a fact about the locks, not about luck.
    """
    import app.routers.emails as emails_router
    from app.database import async_session
    from app.models.email_log import EmailLog, EmailStatus

    async with async_session() as db:
        for i in range(10):
            db.add(EmailLog(
                to_email=f"race{i}@example.com", to_name=f"Buyer {i}",
                subject=f"Outreach {i}", body="<p>x</p>",
                status=EmailStatus.PENDING,
            ))
        await db.commit()

    sent: list[tuple[str, str]] = []
    first_send_started = asyncio.Event()
    release_first = asyncio.Event()

    async def gated_sender(to_email, to_name, subject, body, tracking_id=None):
        sent.append((to_email, subject))
        if not first_send_started.is_set():
            first_send_started.set()
            await release_first.wait()
        return {"success": True, "simulated": True}

    monkeypatch.setattr(emails_router, "send_email", gated_sender)

    async def click():
        async with async_session() as db:
            try:
                return await emails_router.api_send_pending(db=db)
            except Exception as exc:                    # HTTPException(409)
                return {"refused": type(exc).__name__,
                        "status": getattr(exc, "status_code", None)}

    first = asyncio.create_task(click())
    await first_send_started.wait()          # the first call is mid-send
    second = asyncio.create_task(click())
    await asyncio.sleep(0.2)                 # let it reach the lock and be told
    release_first.set()
    results = await asyncio.gather(first, second)

    # Scoped to this test's own drafts: other tests leave pending drafts in the
    # shared database, and `send-pending` is entitled to send those too.
    mine = [entry for entry in sent if entry[0].startswith("race")]
    assert len(mine) == 10, (
        f"{len(mine)} SMTP calls for this test's 10 drafts -- the second call "
        f"sent emails the first had already claimed: {mine}"
    )
    assert len(set(mine)) == len(mine), "the same customer was emailed twice"

    refusals = [r for r in results if r.get("status") == 409]
    successes = [r for r in results if r.get("sent")]
    assert len(refusals) == 1 and len(successes) == 1, results
    assert successes[0]["sent"] >= 10


async def test_a_batch_send_and_a_single_send_cannot_overlap(monkeypatch):
    """Otherwise the batch sends the draft the single call is sending."""
    import app.routers.emails as emails_router
    from app.database import async_session
    from app.models.email_log import EmailLog, EmailStatus

    async with async_session() as db:
        log = EmailLog(
            to_email="both@example.com", to_name="Buyer",
            subject="One draft", body="<p>x</p>", status=EmailStatus.PENDING,
        )
        db.add(log)
        await db.commit()
        log_id = log.id

    sent: list[str] = []

    async def slow_sender(to_email, to_name, subject, body, tracking_id=None):
        await asyncio.sleep(0.05)
        sent.append(to_email)
        return {"success": True, "simulated": True}

    monkeypatch.setattr(emails_router, "send_email", slow_sender)

    async def batch():
        async with async_session() as db:
            try:
                return await emails_router.api_send_pending(db=db)
            except Exception as exc:
                return {"status": getattr(exc, "status_code", None)}

    async def single():
        async with async_session() as db:
            try:
                return await emails_router.api_send_single_pending(email_id=log_id, db=db)
            except Exception as exc:
                return {"status": getattr(exc, "status_code", None)}

    batch_result, single_result = await asyncio.gather(batch(), single())

    assert sent.count("both@example.com") <= 1, sent
    refused = [r for r in (batch_result, single_result) if r.get("status") == 409]
    assert len(refused) == 1, (batch_result, single_result)


async def test_the_refusal_is_a_409_over_http(monkeypatch):
    """A caller has to be able to tell "already sending" from "bad request"."""
    import app.routers.emails as emails_router
    from app.database import async_session
    from app.models.email_log import EmailLog, EmailStatus

    async with async_session() as db:
        db.add(EmailLog(
            to_email="http@example.com", to_name="Buyer",
            subject="Draft", body="<p>x</p>", status=EmailStatus.PENDING,
        ))
        await db.commit()

    async def slow_sender(to_email, to_name, subject, body, tracking_id=None):
        await asyncio.sleep(0.05)
        return {"success": True, "simulated": True}

    monkeypatch.setattr(emails_router, "send_email", slow_sender)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first, second = await asyncio.gather(
            client.post("/api/v1/emails/send-pending"),
            client.post("/api/v1/emails/send-pending"),
        )

    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 409], (first.status_code, second.status_code, first.text, second.text)
    refusal = first if first.status_code == 409 else second
    assert "already in progress" in refusal.json()["detail"]
