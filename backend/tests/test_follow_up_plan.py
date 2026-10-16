"""The follow-up plan: a commitment with a date, not a report recomputed daily.

The defect this replaces, measured on one customer with three unanswered emails:
three consecutive runs of ``daily_follow_up_intelligence`` returned
byte-identical entries, with no due date and no count of how many times we had
already tried. Nothing recorded that we had decided anything, so the same
customer came back every morning forever.

What a plan adds, and what is asserted here:

* every entry has a **due time**, and the scheduled job lists what is *due*;
* the **attempt count** is how many emails have gone unanswered since they last
  spoke, derived from the thread rather than stored;
* past ``max_attempts`` it stops proposing email at all -- the quit rule, which
  is the whole difference between proactive and harassing;
* a reply, a failed delivery or a closed stage **cancels** it;
* the day is **bounded**, and what did not fit is counted rather than dropped.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.follow_up_plan import (
    ACTION_KINDS,
    DEFAULT_FOLLOW_UP_PLAN_POLICY,
    EMAILING_KINDS,
    KIND_ANSWER_THEM,
    KIND_AWAITING_APPROVAL,
    KIND_CHANGE_CHANNEL,
    KIND_FIX_DELIVERY,
    KIND_REVIEW,
    KIND_SEND_FOLLOW_UP,
    CustomerPlanInput,
    plan_follow_ups,
    plan_one,
)
from app.services.follow_up_policy import derive_thread_state

NOW = datetime(2026, 10, 16, 12, 0, 0)
#: "Due now" means due today. Microsecond precision is false precision for a
#: daily plan, and it made two reads of the same state differ.
TODAY = datetime(2026, 10, 16, 0, 0, 0)


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


def state(stage="contacted", *, emails=(), customer_id=1, has_any_email=None, **kwargs):
    return CustomerPlanInput(
        customer_id=customer_id,
        company_name=f"Customer {customer_id}",
        stage=stage,
        thread=derive_thread_state(list(emails), NOW),
        has_any_email=bool(emails) if has_any_email is None else has_any_email,
        **kwargs,
    )


# --------------------------------------------------------------------------
# Due times
# --------------------------------------------------------------------------

def test_a_stage_with_no_email_behind_it_is_due_now():
    action = plan_one(state("contacted", has_any_email=False), now=NOW)

    assert action.kind == KIND_SEND_FOLLOW_UP
    assert action.due_at == TODAY
    assert "no email record" in action.reason
    assert action.attempt == 0


def test_the_reason_names_the_stage_the_customer_is_actually_in():
    """A quoting customer is not "marked as contacted"."""
    action = plan_one(state("quoting", has_any_email=False), now=NOW)

    assert "quoting" in action.reason
    assert "marked as contacted" not in action.reason


def test_the_first_follow_up_is_due_after_the_configured_interval():
    action = plan_one(state(emails=[row(sent_days_ago=4)]), now=NOW)

    assert action.kind == KIND_SEND_FOLLOW_UP
    assert action.due_at == NOW - timedelta(days=4) + timedelta(days=3)
    assert action.attempt == 1


def test_an_email_sent_this_morning_is_not_due_today():
    """The old rule called quoting high priority unconditionally, every day."""
    action = plan_one(state("quoting", emails=[row(sent_days_ago=0)]), now=NOW)

    assert action.due_at > NOW, "a quote sent this morning is not due today"
    plan = plan_follow_ups([state("quoting", emails=[row(sent_days_ago=0)])],
                           now=NOW, due_only=True)
    assert len(plan) == 0


def test_each_attempt_waits_longer_than_the_last():
    first = plan_one(state(emails=[row(sent_days_ago=10)]), now=NOW)
    second = plan_one(state(emails=[row(sent_days_ago=10), row(sent_days_ago=7)]), now=NOW)

    assert first.attempt == 1 and second.attempt == 2
    gap_first = (first.due_at - (NOW - timedelta(days=10))).days
    gap_second = (second.due_at - (NOW - timedelta(days=7))).days
    assert gap_first == 3
    assert gap_second == 6, "the second chase waits longer than the first"


# --------------------------------------------------------------------------
# The quit rule
# --------------------------------------------------------------------------

def test_at_the_attempt_cap_the_plan_stops_proposing_email():
    emails = [row(sent_days_ago=20), row(sent_days_ago=14), row(sent_days_ago=8)]
    action = plan_one(state(emails=emails), now=NOW)

    assert action.kind == KIND_CHANGE_CHANNEL
    assert action.attempt == 3
    assert not action.emails_them_again, (
        "past the cap the plan must not propose another email; that is how a "
        "quit rule turns back into a fourth message"
    )
    assert "another email is unlikely to work" in action.reason


def test_past_the_cap_the_customer_is_parked_rather_than_dropped():
    emails = [row(sent_days_ago=d) for d in (40, 34, 28, 22)]
    action = plan_one(state(emails=emails), now=NOW)

    assert action.kind == KIND_REVIEW
    assert action.priority == "low"
    assert action.due_at > NOW + timedelta(days=30), "reviewed later, not forgotten"


def test_the_quit_rule_is_tunable_and_named():
    emails = [row(sent_days_ago=20), row(sent_days_ago=14), row(sent_days_ago=8)]
    patient = plan_one(
        state(emails=emails), now=NOW,
        policy=DEFAULT_FOLLOW_UP_PLAN_POLICY.__class__(max_attempts=5),
    )

    assert patient.kind == KIND_SEND_FOLLOW_UP
    assert patient.attempt == 3


def test_a_reply_they_sent_outranks_everything_else():
    action = plan_one(
        state("quoting", emails=[row(sent_days_ago=8, replied_days_ago=2)]), now=NOW
    )

    assert action.kind == KIND_ANSWER_THEM
    assert action.due_at == NOW - timedelta(days=2), "overdue the moment they wrote"
    assert action.priority == "high"
    assert "have not heard back" in action.reason


def test_answering_them_is_not_an_emailing_entry_that_could_be_skipped():
    """It is the one entry whose next action is never in doubt."""
    action = plan_one(state(emails=[row(sent_days_ago=8, replied_days_ago=2)]), now=NOW)
    assert action.emails_them_again is False
    assert action.kind == KIND_ANSWER_THEM


def test_a_failed_send_is_not_an_unanswered_email():
    """Waiting out a follow-up interval for something that never arrived is
    waiting for nothing."""
    action = plan_one(
        state(emails=[row(sent_days_ago=0, status="failed")],
              last_failed_at=NOW, last_failed_reason="smtp refused"),
        now=NOW,
    )

    assert action.kind == KIND_FIX_DELIVERY
    assert action.due_at == TODAY
    assert action.priority == "high"
    assert "smtp refused" in action.reason


def test_a_closed_customer_is_not_planned_for():
    for stage in ("completed", "lost"):
        assert plan_one(state(stage), now=NOW) is None


def test_an_interested_customer_is_due_after_the_interval_not_every_day():
    """The old rule called `interested` medium priority unconditionally.

    Now it is one email with a due date, so a customer contacted this morning is
    absent from today's list instead of appearing every morning forever.
    """
    action = plan_one(state("interested", emails=[row(sent_days_ago=5)]), now=NOW)

    assert action.kind == KIND_SEND_FOLLOW_UP
    assert action.due_at == NOW - timedelta(days=5) + timedelta(days=3)
    assert action.priority == "medium"


def test_a_draft_waiting_for_approval_suspends_the_customer():
    """The state is *parked*, not due.

    A draft in `pending` is a proposal waiting for a human. The plan says so,
    stops proposing anything else for that customer, and does not treat the
    customer as work the agent owes -- the next move is a person's.
    """
    action = plan_one(state("quoting", emails=[row(status="pending")]), now=NOW)

    assert action.kind == KIND_AWAITING_APPROVAL
    assert action.kind not in EMAILING_KINDS, "you cannot propose a proposal"
    assert action.due_at > NOW, "suspended: not due until the reminder window"
    assert "waiting for your approval" in action.reason
    assert action.suggested_action.startswith("Review the draft")

    # Nothing due today, because nothing is owed by the agent today.
    plan = plan_follow_ups(
        [state("quoting", emails=[row(status="pending")])], now=NOW, due_only=True
    )
    assert len(plan) == 0


def test_a_proposal_that_has_been_waiting_long_enough_reminds_the_human():
    stale = plan_one(
        state(emails=[row(sent_days_ago=9, status="pending")]), now=NOW
    )
    fresh = plan_one(
        state(emails=[row(sent_days_ago=0, status="pending")]), now=NOW
    )

    assert stale.due_at <= NOW, "past the reminder window it is worth raising"
    assert stale.priority == "high"
    assert fresh.priority == "medium"


def test_a_pending_proposal_outranks_answering_them_because_it_may_be_the_answer():
    """They wrote last *and* a draft is waiting: the draft is probably the reply.

    The entry keeps both facts, so the urgency is not lost -- but the next move
    is still the human's, and the plan says that instead of proposing new
    outreach on top of an unanswered proposal.
    """
    action = plan_one(
        state(emails=[row(sent_days_ago=4, replied_days_ago=2, status="pending")]),
        now=NOW,
    )

    assert action.kind == KIND_AWAITING_APPROVAL
    assert "waiting for your approval" in action.reason
    assert "they replied" in action.reason
    assert action.priority == "high"


def test_the_plan_stops_proposing_while_a_proposal_is_pending():
    """The defect this replaces: the entry was identical to having no draft.

    Measured: with one email sent ten days ago plus a pending draft, the plan
    produced the same kind, the same due date and the same reason as with no
    draft at all -- so the daily list told a human to chase a customer whose
    email was already written and sitting in their approval queue.
    """
    with_proposal = plan_one(
        state(emails=[row(sent_days_ago=10), row(status="pending")]), now=NOW
    )
    without = plan_one(state(emails=[row(sent_days_ago=10)]), now=NOW)

    assert with_proposal.kind == KIND_AWAITING_APPROVAL
    assert without.kind == KIND_SEND_FOLLOW_UP
    assert with_proposal.kind != without.kind, (
        "a proposal waiting for approval must not read as work still to do"
    )
    assert with_proposal.as_dict() != without.as_dict()


def test_a_rejected_proposal_is_remembered_and_changes_the_advice():
    action = plan_one(
        state(emails=[row(sent_days_ago=3, status="rejected")]), now=NOW
    )

    assert action.kind == KIND_SEND_FOLLOW_UP
    assert "rejected" in action.reason
    assert action.evidence["rejected_proposals"] == 1
    assert action.evidence["pending_proposals"] == 0


def test_every_declared_action_kind_is_reachable():
    """A kind no state can produce is decoration, not a vocabulary.

    An earlier draft carried a "keep_momentum" kind that no reachable state could
    emit: every path that would have produced it was already handled by the
    rules above it. Asserted rather than noticed.
    """
    produced = {
        plan_one(state("contacted", has_any_email=False), now=NOW).kind,
        plan_one(state(emails=[row(sent_days_ago=4)]), now=NOW).kind,
        plan_one(state(emails=[row(sent_days_ago=0, status="pending")]), now=NOW).kind,
        plan_one(state(emails=[row(sent_days_ago=20), row(sent_days_ago=14),
                               row(sent_days_ago=8)]), now=NOW).kind,
        plan_one(state(emails=[row(sent_days_ago=d) for d in (40, 34, 28, 22)]),
                 now=NOW).kind,
        plan_one(state(emails=[row(sent_days_ago=8, replied_days_ago=2)]), now=NOW).kind,
        plan_one(state(emails=[row(sent_days_ago=0, status="failed")],
                       last_failed_at=NOW), now=NOW).kind,
    }

    assert produced == set(ACTION_KINDS)


# --------------------------------------------------------------------------
# The day is bounded, ordered and deterministic
# --------------------------------------------------------------------------

def test_the_day_is_capped_and_what_did_not_fit_is_counted():
    states = [
        state(emails=[row(sent_days_ago=10)], customer_id=i) for i in range(25)
    ]
    plan = plan_follow_ups(states, now=NOW)

    assert len(plan) == 20
    assert plan.deferred == 5, "a truncated list must not read as a complete one"


def test_the_most_overdue_entry_comes_first():
    states = [
        state(emails=[row(sent_days_ago=4)], customer_id=1),   # 1 day overdue
        state(emails=[row(sent_days_ago=30)], customer_id=2),  # 27 days overdue
        state(emails=[row(sent_days_ago=9)], customer_id=3),   # 6 days overdue
    ]
    plan = plan_follow_ups(states, now=NOW)

    assert [a.customer_id for a in plan] == [2, 3, 1]


def test_the_same_state_always_produces_the_same_plan():
    states = [state(emails=[row(sent_days_ago=10)], customer_id=i) for i in range(5)]
    first = plan_follow_ups(states, now=NOW).as_dict()
    second = plan_follow_ups(states, now=NOW).as_dict()

    assert first == second, "the schedule is arithmetic, not a judgement call"


def test_due_only_is_what_makes_tomorrow_incremental():
    """A commitment scheduled forward does not come back today."""
    states = [
        state(emails=[row(sent_days_ago=0)], customer_id=1),     # due in 3 days
        state(emails=[row(sent_days_ago=30)], customer_id=2),    # overdue
    ]
    everything = plan_follow_ups(states, now=NOW)
    today = plan_follow_ups(states, now=NOW, due_only=True)

    assert len(everything) == 2
    assert [a.customer_id for a in today] == [2]


def test_the_plan_carries_the_evidence_it_was_computed_from():
    action = plan_one(
        state(emails=[row(sent_days_ago=20), row(sent_days_ago=8, replied_days_ago=2)]),
        now=NOW,
    )
    evidence = action.as_dict()["evidence"]

    assert evidence["answered"] is True
    assert evidence["superseded"] == 1
    assert evidence["they_wrote_last"] is True
    assert evidence["days_since_their_last_reply"] == 2


# --------------------------------------------------------------------------
# Over HTTP: the plan is committed, and reading it changes nothing
# --------------------------------------------------------------------------

def send_three(client, customer_id=4, to_email="y@e.jp"):
    ids = []
    for subject in ("cold outreach", "follow-up 1", "follow-up 2"):
        response = client.post("/api/v1/emails/send", json={
            "customer_id": customer_id, "to_email": to_email,
            "subject": subject, "body": "<p>x</p>",
        })
        assert response.status_code == 200, response.text
        ids.append(response.json()["id"])
    return ids


def customer_row(client, customer_id):
    items = client.get("/api/v1/customers/").json()["items"]
    return next(item for item in items if item["id"] == customer_id)


def plan_for(client, customer_id):
    """This customer's entry in the full queue, or None."""
    plan = client.get("/api/v1/agent/follow-up-plan").json()
    return next((a for a in plan["actions"] if a["customer_id"] == customer_id), None)


def due_today(client):
    return client.post("/api/v1/agent/daily-intelligence?due_only=true").json()


def test_reading_the_plan_commits_nothing(client):
    send_three(client)
    before = customer_row(client, 4)["next_follow_up_at"]

    first = client.get("/api/v1/agent/follow-up-plan").json()
    second = client.get("/api/v1/agent/follow-up-plan").json()

    # `as_of` is a timestamp and moves; the plan itself must not.
    assert first["actions"] == second["actions"]
    assert first["by_kind"] == second["by_kind"]
    assert customer_row(client, 4)["next_follow_up_at"] == before


def test_running_the_plan_commits_the_quit_decision_and_stops_repeating_it(client):
    """The exact defect: three unanswered emails came back every morning.

    Asserted relative to whatever history the shared test database already
    holds, because the property is "three more unanswered emails produce a
    decision and a future date", not "the count is exactly three".
    """
    before = plan_for(client, 4)
    baseline = before["attempt"] if before else 0

    send_three(client)

    after = plan_for(client, 4)
    assert after["attempt"] >= baseline + 3
    assert after["kind"] in (KIND_CHANGE_CHANNEL, KIND_REVIEW), (
        "past the cap the plan must stop proposing email"
    )
    assert after["due_in_days"] > 0
    assert "draft_email" not in after, (
        "a change-the-channel entry must not arrive with a ready-to-send email"
    )

    # The scheduled job has nothing for this customer today, and still nothing
    # after another run: the decision was committed, not recomputed.
    assert [a for a in due_today(client)["actions"] if a["customer_id"] == 4] == []
    assert [a for a in due_today(client)["actions"] if a["customer_id"] == 4] == []

    committed = customer_row(client, 4)["next_follow_up_at"]
    assert committed is not None, "the column existed and was never written"
    assert datetime.fromisoformat(committed) > datetime.utcnow()


def test_a_reply_cancels_the_chase_and_the_plan_switches_to_answering_them(client):
    ids = send_three(client)
    due_today(client)

    reply = client.post(f"/api/v1/emails/logs/{ids[-1]}/reply")
    assert reply.status_code == 200

    entry = plan_for(client, 4)
    assert entry["kind"] == KIND_ANSWER_THEM
    assert entry["attempt"] == 0
    assert "have not heard back" in entry["reason"]
    # And it is due immediately, because they are waiting.
    assert entry["due_at"] <= due_today(client)["as_of"]


def test_the_agent_can_read_the_plan_without_changing_it(client):
    """The proactive path: the scheduler commits, the agent reads and explains."""
    import asyncio

    from app.agent.trade_tools import build_trade_registry

    before = customer_row(client, 4)["next_follow_up_at"]
    registry = build_trade_registry()
    assert "get_follow_up_plan" in registry

    result, _ = asyncio.run(registry.call("get_follow_up_plan", {"limit": 3}))

    assert result.ok, result.observation
    body = result.raw
    assert body["total_actions"] >= 1
    assert len(body["actions"]) <= 3
    assert "kind" in body["actions"][0]
    assert body["deferred"] >= 0
    # Reading is not committing.
    assert customer_row(client, 4)["next_follow_up_at"] == before


def test_overdue_items_keep_appearing_until_someone_does_them(client):
    """Repeating an overdue item is not the bug; repeating a scheduled one was.

    Customers 1 and 2 have stages and no email behind them, so the action is
    genuinely owed today and stays owed. A plan that hid them would be a plan
    that forgets.
    """
    first = client.post("/api/v1/agent/daily-intelligence?due_only=true").json()
    second = client.post("/api/v1/agent/daily-intelligence?due_only=true").json()

    owed = {a["customer_id"] for a in first["actions"]}
    again = {a["customer_id"] for a in second["actions"]}
    assert owed and owed == again
    for action in second["actions"]:
        assert action["due_at"] <= second["as_of"]
        assert action["priority"] in ("high", "medium")
