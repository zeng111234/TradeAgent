"""Debugging one customer's handling.

The question a person actually asks is "why did Martin get nothing this week",
and it starts from the customer. Before this endpoint that trip was not possible:
the facts lived in four places that did not reference each other, and the one
place that could have linked them -- ``agent_runs`` -- carried no customer
reference at all, only a free-text goal and a JSON trace.

Two properties this file exists to hold:

* **It is read-only.** The obvious way to inspect the plan is
  ``POST /agent/daily-intelligence``, which *writes* the commitments it computes.
  Investigating with it changes the thing under investigation, and you cannot
  look twice. The debug view builds the plan with ``persist=False``.
* **It does not confuse customers.** Run traces are matched on the parsed
  ``customer_id`` argument, not on a substring of the JSON: ``4`` appears inside
  ``14`` and ``40``, and a debug view that shows another customer's run is worse
  than showing none.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.email_log import EmailLog, EmailStatus
from app.services.customer_debug import debug_customer, tool_calls_naming

URL = "/api/v1/customers/{}/debug"


@pytest.fixture
def temp_customer(db):
    """A throwaway customer that is **deleted again** when the test ends.

    The follow-up plan's day is ``max_actions_per_day`` bounded -- the first
    twenty by due date -- so a customer left behind is not merely clutter: it
    holds a slot in a shared, capped resource. Two tests in another file went red
    because this file's customers crowded the demo customer out of that list, and
    a third helper assumed the whole book still fitted on one page of
    ``GET /customers/``.

    Deleting them again is the honest fix. A closed-stage customer dodges the
    plan but still grows the book, and "my test data is special" is how the next
    person loses an afternoon.
    """
    import asyncio

    from app.models.customer import Customer

    created: list[int] = []

    def factory(tag="debug", **kwargs):
        customer_id = asyncio.run(make_customer(db, tag, **kwargs))
        created.append(customer_id)
        return customer_id

    yield factory

    async def cleanup():
        for customer_id in created:
            customer = (
                await db.execute(select(Customer).where(Customer.id == customer_id))
            ).scalar_one_or_none()
            if customer is not None:
                # Cascades through contacts, notes and email_logs.
                await db.delete(customer)
        await db.commit()

    asyncio.run(cleanup())


@pytest.fixture
def temp_run(db):
    """An agent run that is deleted again when the test ends.

    Same reason as ``temp_customer``, plus a subtlety that cost a round of
    debugging: the debug view finds runs by the customer id **inside** their
    trace and goal, and SQLite hands a deleted row's id straight to the next
    insert. A run left behind about customer 7 therefore becomes, a moment later,
    evidence that the *new* customer 7 has a history -- and a test asserting "no
    run has ever touched this customer" fails for a reason that has nothing to do
    with the code under test.
    """
    import asyncio

    from app.models.agent_run import AgentRun

    created: list[str] = []

    def factory(**fields):
        run_id = uuid.uuid4().hex
        db.add(AgentRun(run_id=run_id, **fields))
        asyncio.run(db.commit())
        created.append(run_id)
        return run_id

    yield factory

    async def cleanup():
        for run_id in created:
            run = (
                await db.execute(select(AgentRun).where(AgentRun.run_id == run_id))
            ).scalar_one_or_none()
            if run is not None:
                await db.delete(run)
        await db.commit()

    asyncio.run(cleanup())


async def make_customer(db, tag="debug", *, stage="completed", contact=True):
    """A throwaway customer, **closed by default**.

    Closed so the follow-up plan does not see it. Plan-eligible customers occupy
    the plan's ``max_actions_per_day`` slots -- the day's list is the first twenty
    by due date -- and this file creates one customer per test. Left eligible,
    they pushed the demo customer out of the reply test in another file and
    turned it red, which is the third time in this project that test data has
    been the thing under test.

    The tests that need a plan entry ask for ``stage="contacted"`` explicitly and
    give the customer a due date in the past, so it sorts with the early entries
    instead of competing with them.
    """
    from app.models.customer import Customer, CustomerContact

    suffix = uuid.uuid4().hex[:8]
    customer = Customer(
        company_name=f"Debug {tag} {suffix}",
        country="Germany",
        industry="textiles",
        stage=stage,
        source="manual",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)

    if contact:
        db.add(CustomerContact(
            customer_id=customer.id, name="Frau Meier",
            email=f"{tag}-{suffix}@example.test", is_primary=1,
        ))
        await db.commit()
    return customer.id


async def add_email(db, customer_id, *, status, subject="Quote", days_ago=3,
                    error=None, replied_days_ago=None):
    sent = datetime.utcnow() - timedelta(days=days_ago)
    row = EmailLog(
        customer_id=customer_id,
        to_email="buyer@example.test",
        subject=subject,
        body="<p>x</p>",
        status=status,
        tracking_id=f"t-{uuid.uuid4().hex[:10]}",
        created_at=sent,
        sent_at=sent if status not in (EmailStatus.PENDING, EmailStatus.FAILED) else None,
        replied_at=(
            datetime.utcnow() - timedelta(days=replied_days_ago)
            if replied_days_ago is not None else None
        ),
        error_message=error,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


# --------------------------------------------------------------------------
# The trip from customer to facts
# --------------------------------------------------------------------------

def test_a_customer_with_a_failed_send_says_so(client, db, temp_customer):
    import asyncio

    customer_id = temp_customer("failed", stage="contacted")
    asyncio.run(add_email(
        db, customer_id, status=EmailStatus.FAILED, days_ago=1,
        error="SMTPAuthenticationError: 535 bad credentials",
    ))

    body = client.get(URL.format(customer_id)).json()

    assert body["customer"]["id"] == customer_id
    assert body["emails"][0]["status"] == "failed"
    assert body["emails"][0]["error_message"].startswith("SMTPAuthentication")

    joined = " | ".join(body["findings"])
    assert "535 bad credentials" in joined, "the finding must carry the actual error"
    assert body["plan"]["kind"] == "fix_delivery"


def test_a_reply_nobody_answered_for_three_months_is_explained(client, db, temp_customer):
    import asyncio

    customer_id = temp_customer("stale", stage="contacted")
    asyncio.run(add_email(
        db, customer_id, status=EmailStatus.REPLIED, days_ago=100,
        replied_days_ago=90,
    ))

    body = client.get(URL.format(customer_id)).json()

    assert body["thread"]["they_wrote_last"] is True
    assert body["thread"]["reply_owed_days"] == 90
    assert body["plan"]["kind"] == "review"
    joined = " | ".join(body["findings"])
    assert "90 day(s) ago" in joined
    assert "decision" in joined


def test_a_customer_with_no_address_is_diagnosed_not_just_listed(client, db, temp_customer):
    import asyncio

    customer_id = temp_customer("noemail", contact=False)

    body = client.get(URL.format(customer_id)).json()

    assert body["contact"] is None
    assert "No email address on file" in " | ".join(body["findings"])


def test_a_closed_customer_says_why_the_plan_ignores_them(client, db, temp_customer):
    import asyncio

    customer_id = temp_customer("closed", stage="completed")
    asyncio.run(add_email(db, customer_id, status=EmailStatus.SENT))

    body = client.get(URL.format(customer_id)).json()

    assert body["plan"] is None
    assert "closed stage" in " | ".join(body["findings"])


def test_the_plan_decision_comes_with_the_evidence_it_used(client, db, temp_customer):
    import asyncio

    customer_id = temp_customer("evidence", stage="contacted")
    asyncio.run(add_email(db, customer_id, status=EmailStatus.SENT, days_ago=10))

    body = client.get(URL.format(customer_id)).json()

    assert body["plan"] is not None
    assert body["plan"]["evidence"], "a decision without its numbers cannot be checked"


def test_unknown_customer_is_a_404(client):
    assert client.get(URL.format(999999)).status_code == 404


# --------------------------------------------------------------------------
# Reading it must not change it
# --------------------------------------------------------------------------

def test_debugging_does_not_write_the_commitment_it_inspects(client, db, temp_customer):
    """The whole reason this is a GET.

    ``POST /agent/daily-intelligence`` persists ``next_follow_up_at``, so using
    it to investigate would move the customer's schedule as a side effect of
    looking at it.
    """
    import asyncio

    from app.models.customer import Customer

    customer_id = temp_customer("readonly", stage="contacted")
    asyncio.run(add_email(db, customer_id, status=EmailStatus.SENT, days_ago=10))

    before = asyncio.run(_commitment(db, customer_id))
    email_count_before = asyncio.run(_email_count(db, customer_id))

    client.get(URL.format(customer_id))
    client.get(URL.format(customer_id))

    assert asyncio.run(_commitment(db, customer_id)) == before, (
        "looking at the customer moved their follow-up commitment"
    )
    assert asyncio.run(_email_count(db, customer_id)) == email_count_before, (
        "looking at the customer created a draft"
    )


def test_the_writing_endpoint_does_move_it(client, db, temp_customer):
    """The contrast, so the test above is not vacuous.

    If this ever stops writing, the read-only property above stops meaning
    anything -- it would be true of both paths.

    It has to put the book back afterwards. ``POST /agent/daily-intelligence``
    commits a follow-up date for **every** customer, and leaving those written
    changes what other tests see: three tests about "reading the plan commits
    nothing" failed the first time this ran, because a test in this file had
    quietly committed the whole book. A test that demonstrates a write is fine;
    a test that leaves the write behind is not.
    """
    import asyncio

    from app.models.customer import Customer

    customer_id = temp_customer("writes", stage="contacted")
    asyncio.run(add_email(db, customer_id, status=EmailStatus.SENT, days_ago=10))

    async def snapshot():
        rows = (await db.execute(select(Customer))).scalars().all()
        return {row.id: row.next_follow_up_at for row in rows}

    before = asyncio.run(snapshot())
    client.post("/api/v1/agent/daily-intelligence")

    assert asyncio.run(_commitment(db, customer_id)) != before[customer_id], (
        "the POST no longer commits anything, which makes the read-only test vacuous"
    )

    async def restore():
        rows = (await db.execute(select(Customer))).scalars().all()
        for row in rows:
            if row.id in before:
                row.next_follow_up_at = before[row.id]
        await db.commit()

    asyncio.run(restore())
    assert asyncio.run(snapshot()) == before, "the book was not put back"


async def _commitment(db, customer_id):
    from app.models.customer import Customer

    customer = (
        await db.execute(select(Customer).where(Customer.id == customer_id))
    ).scalar_one()
    await db.refresh(customer)
    return customer.next_follow_up_at


async def _email_count(db, customer_id):
    from sqlalchemy import func

    return (
        await db.execute(
            select(func.count(EmailLog.id)).where(EmailLog.customer_id == customer_id)
        )
    ).scalar_one()


# --------------------------------------------------------------------------
# Finding the runs that touched them
# --------------------------------------------------------------------------

def test_tool_calls_are_matched_on_the_argument_not_the_text():
    """id 4 must not match a run about 14 or 40."""
    trace = json.dumps([
        {
            "round": 1,
            "tool_calls": [
                {"name": "get_customer", "arguments": {"customer_id": 14}, "ok": True},
                {"name": "get_customer_signals", "arguments": {"customer_id": 4}, "ok": True},
                {"name": "list_customers", "arguments": {"stage": "quoting"}, "ok": True},
            ],
        }
    ])

    naming = tool_calls_naming(trace, 4)

    assert [c["tool"] for c in naming] == ["get_customer_signals"], (
        "a substring match on the JSON would also have picked up customer 14"
    )


def test_a_malformed_trace_does_not_break_the_debug_view():
    assert tool_calls_naming("not json at all", 4) == []
    assert tool_calls_naming(None, 4) == []
    assert tool_calls_naming(json.dumps({"not": "a list"}), 4) == []


def test_a_run_that_touched_the_customer_is_found(client, db, temp_customer, temp_run):
    import asyncio

    from app.models.agent_run import AgentRun

    customer_id = temp_customer("runs")
    trace = json.dumps([
        {
            "round": 1,
            "tool_calls": [
                {"name": "get_customer", "arguments": {"customer_id": customer_id},
                 "ok": True},
            ],
        }
    ])

    def seed():
        temp_run(
            goal=f"review customer {customer_id}",
            status="error",
            stop_reason="llm_error",
            error="the provider hung up",
            trace=trace,
            rounds=1,
            tool_calls=1,
        )

    seed()

    body = client.get(URL.format(customer_id)).json()

    assert body["runs"], "the run that touched this customer was not found"
    run = body["runs"][0]
    assert run["status"] == "error"
    assert run["error"] == "the provider hung up"
    assert run["tool_calls_naming_this_customer"][0]["tool"] == "get_customer"
    assert run["trace_url"].startswith("/api/v1/agent/runs/")


def test_a_run_started_with_a_customer_is_found_by_the_column(client, db, temp_customer, temp_run):
    """The indexed path: no trace scanning, no guesswork.

    Migration ``0001_agent_runs_customer_id`` added the column, and this asserts
    it is actually used. The run's trace says nothing about the customer -- a run
    can be *about* somebody without any tool call naming them -- and it is found
    anyway, because the caller said who it was about.
    """
    customer_id = temp_customer("column")
    temp_run(
        goal="review this account and draft a follow-up",
        status="succeeded",
        customer_id=customer_id,
        trace=json.dumps([{"round": 1, "tool_calls": []}]),
        rounds=1,
        tool_calls=0,
    )

    body = client.get(URL.format(customer_id)).json()

    assert body["runs"], "a run that named the customer was not found"
    assert body["runs"][0]["matched_by"] == "customer_id column"
    assert body["runs_found_by_column"] == 1


def test_a_legacy_run_is_still_found_by_its_trace(client, db, temp_customer, temp_run):
    """Rows written before the column existed are real history.

    They have no ``customer_id`` to read, so they are still found the old way.
    Dropping them would make the view lie by omission. Both paths are exercised
    so neither can be deleted as dead code.
    """
    customer_id = temp_customer("legacy")
    temp_run(
        goal="old style run",
        status="succeeded",
        customer_id=None,
        trace=json.dumps([
            {"round": 1, "tool_calls": [
                {"name": "get_customer", "arguments": {"customer_id": customer_id},
                 "ok": True},
            ]},
        ]),
        rounds=1,
        tool_calls=1,
    )

    body = client.get(URL.format(customer_id)).json()

    assert body["runs"], "the legacy run was lost by the migration"
    assert body["runs"][0]["matched_by"] == "tool arguments"
    assert body["runs_found_by_column"] == 0
    assert body["legacy_runs_searched"] >= 1


def test_a_run_about_another_customer_is_not_shown(client, db, temp_customer, temp_run):
    customer_id = temp_customer("mine")
    other_id = temp_customer("theirs")

    temp_run(
        goal="analyse a completely different account",
        status="succeeded",
        trace=json.dumps([
            {"round": 1, "tool_calls": [
                {"name": "get_customer", "arguments": {"customer_id": other_id},
                 "ok": True},
            ]},
        ]),
        rounds=1,
        tool_calls=1,
    )

    body = client.get(URL.format(customer_id)).json()

    assert body["runs"] == [], (
        "another customer's run appeared in this customer's story"
    )


def test_saying_no_run_ever_touched_them_is_itself_the_answer(client, db, temp_customer, temp_run):
    import asyncio

    customer_id = temp_customer("untouched")
    asyncio.run(add_email(db, customer_id, status=EmailStatus.SENT, days_ago=30))

    body = client.get(URL.format(customer_id)).json()

    assert "No agent run has ever mentioned this customer" in " | ".join(body["findings"])


# --------------------------------------------------------------------------
# The view is bounded, and says so
# --------------------------------------------------------------------------

def test_the_view_is_capped_and_reports_the_cap(client, db, temp_customer):
    import asyncio

    from app.services import customer_debug

    customer_id = temp_customer("many")

    async def seed():
        for index in range(customer_debug.MAX_EMAILS + 5):
            await add_email(db, customer_id, status=EmailStatus.SENT,
                            subject=f"Message {index}")

    asyncio.run(seed())

    body = client.get(URL.format(customer_id)).json()

    assert body["emails_shown"] == customer_debug.MAX_EMAILS
    assert len(body["emails"]) == customer_debug.MAX_EMAILS


def test_the_debug_view_is_json_serialisable(client, db, temp_customer):
    """It goes over HTTP, so a datetime leaking into it is a 500."""
    import asyncio

    customer_id = temp_customer("serialise")
    asyncio.run(add_email(db, customer_id, status=EmailStatus.SENT))

    response = client.get(URL.format(customer_id))

    assert response.status_code == 200, response.text
    json.dumps(response.json())


async def test_the_service_returns_none_for_a_missing_customer(db):
    assert await debug_customer(db, 999999) is None
