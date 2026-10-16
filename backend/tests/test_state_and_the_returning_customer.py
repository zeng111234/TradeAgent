"""State: where it lives, and what a customer who comes back after three months gets.

The answer to "where is the state" is not one place, and the interesting part is
which of them is authoritative:

* **The database is the state.** One SQLite file. ``email_logs`` is the record of
  every conversation -- status, ``sent_at``, ``replied_at`` -- and
  ``customers.next_follow_up_at`` is the only *commitment* column: the plan writes
  what it decided so the next run is incremental rather than a recomputation.
* **The thread state is not stored at all.** ``derive_thread_state`` reads it out
  of the email rows on every run, purely. That is deliberate: a stored
  ``is_waiting_for_reply`` flag is a second copy of the truth and drifts from the
  first one the moment a reply is recorded and the flag is forgotten.
* **What is *not* state**: the process-local send lock (a courtesy, not a
  guarantee), and APScheduler's job store (the jobs are registered from code at
  startup, so a restart loses nothing).

Three months later, two different things can happen, and they need different
answers:

1. **They write.** Then ``replied_at`` is recent, ``they_wrote_last`` is true, and
   this is an ordinary "answer them" -- provided the human records *when they
   wrote* rather than when they noticed. The endpoint has always accepted
   ``replied_at``; the UI used to always send "now", which turned a three-month-old
   message into a live one.
2. **Nobody answers for three months.** Then the plan used to keep proposing
   "answer their message", priority high, forever -- the same action it proposes
   for a reply that arrived this morning. A message that old is not a reply to
   send; it is a decision (restart with an apology, or close the customer).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.follow_up_plan import (
    DEFAULT_FOLLOW_UP_PLAN_POLICY,
    EMAILING_KINDS,
    KIND_ANSWER_THEM,
    KIND_REVIEW,
    CustomerPlanInput,
    plan_one,
)
from app.services.follow_up_policy import derive_thread_state

NOW = datetime(2026, 6, 1, 9, 0, 0)


class Row:
    """A stand-in for one email_logs row. The derivation only reads attributes."""

    def __init__(self, *, status="sent", sent_at=None, replied_at=None, created_at=None):
        self.status = status
        self.sent_at = sent_at
        self.replied_at = replied_at
        self.created_at = created_at or sent_at


def thread_with_reply_owed(days: int, **overrides):
    """One email sent, answered ``days`` ago, nothing since."""
    sent = NOW - timedelta(days=days + 10)
    replied = NOW - timedelta(days=days)
    thread = derive_thread_state(
        [Row(status="replied", sent_at=sent, replied_at=replied)], NOW
    )
    for key, value in overrides.items():
        object.__setattr__(thread, key, value)
    return thread


def action_for(thread, **overrides):
    fields = dict(
        customer_id=1,
        company_name="Nordstickerei Handel GmbH",
        stage="contacted",
        thread=thread,
        contact_email="einkauf@nordstickerei.example",
        contact_name="Frau Meier",
    )
    fields.update(overrides)
    return plan_one(CustomerPlanInput(**fields), now=NOW, policy=DEFAULT_FOLLOW_UP_PLAN_POLICY)


# --------------------------------------------------------------------------
# The derivation is where the state comes from
# --------------------------------------------------------------------------

def test_the_thread_state_is_derived_from_the_rows_not_stored():
    thread = derive_thread_state(
        [
            Row(status="sent", sent_at=NOW - timedelta(days=40)),
            Row(status="replied", sent_at=NOW - timedelta(days=30),
                replied_at=NOW - timedelta(days=3)),
        ],
        NOW,
    )

    assert thread.they_wrote_last is True
    assert thread.reply_owed_days == 3
    assert thread.awaiting_reply == 0, "the one they answered is not awaiting an answer"


# --------------------------------------------------------------------------
# Case 1: they come back, and the reply is recorded when it happened
# --------------------------------------------------------------------------

def test_a_fresh_reply_is_answered():
    action = action_for(thread_with_reply_owed(0))

    assert action.kind == KIND_ANSWER_THEM
    assert action.priority == "high"
    assert "have not heard back" in action.reason


def test_a_reply_owed_for_a_week_is_still_answered_but_marked_late():
    action = action_for(thread_with_reply_owed(7))

    assert action.kind == KIND_ANSWER_THEM
    assert "past the window" in action.reason


def test_the_day_before_the_threshold_is_still_an_answer():
    """The boundary, asserted rather than assumed."""
    action = action_for(thread_with_reply_owed(29))

    assert action.kind == KIND_ANSWER_THEM
    assert "too late" not in action.reason


# --------------------------------------------------------------------------
# Case 2: three months of silence on our side
# --------------------------------------------------------------------------

def test_a_reply_nobody_answered_for_three_months_is_a_decision_not_an_email():
    action = action_for(thread_with_reply_owed(90))

    assert action.kind == KIND_REVIEW, (
        "proposing a routine answer for a three-month-old message sends a "
        "follow-up into a conversation that ended a season ago"
    )
    assert "90 day(s) ago" in action.reason
    assert "too late for a routine reply" in action.reason
    assert "apolog" in action.suggested_action
    assert "close" in action.suggested_action


def test_the_threshold_itself_flips_the_kind():
    assert action_for(thread_with_reply_owed(29)).kind == KIND_ANSWER_THEM
    assert action_for(thread_with_reply_owed(30)).kind == KIND_REVIEW


def test_a_stale_reply_does_not_rank_below_live_work():
    """It is a broken promise, not a to-do. High, and due immediately."""
    action = action_for(thread_with_reply_owed(90))

    assert action.priority == "high"
    assert action.due_at.date() <= NOW.date(), "an overdue decision is overdue now"


def test_no_email_draft_is_written_for_a_stale_reply():
    """An apology is not something to generate automatically.

    The plan only drafts for ``EMAILING_KINDS``; ``review`` is not one of them,
    which is what keeps this change from turning into an auto-generated apology.
    """
    assert KIND_REVIEW not in EMAILING_KINDS
    assert KIND_ANSWER_THEM not in EMAILING_KINDS


def test_a_waiting_draft_still_wins_over_a_stale_reply():
    """Order matters: the suspension rule is checked before the answer rule.

    If a draft is already sitting in the approval queue, that is the thing to
    decide about -- it may well *be* the answer to the message.
    """
    thread = thread_with_reply_owed(
        90,
        pending_proposals=1,
        pending_proposal_days=1,
        oldest_pending_at=NOW - timedelta(days=1),
    )

    action = action_for(thread)

    assert action.kind == "awaiting_approval"


def test_the_age_comes_from_the_rows_so_a_late_entry_is_recorded_late():
    """The reason the UI has to send ``replied_at``.

    Recording a three-month-old message today makes ``reply_owed_days`` zero, and
    nothing downstream can tell the difference -- the derivation reads the
    timestamp, and the timestamp is a claim about when they wrote.
    """
    sent = NOW - timedelta(days=120)
    recorded_now = derive_thread_state(
        [Row(status="replied", sent_at=sent, replied_at=NOW)], NOW
    )
    recorded_honestly = derive_thread_state(
        [Row(status="replied", sent_at=sent, replied_at=NOW - timedelta(days=90))], NOW
    )

    assert recorded_now.reply_owed_days == 0
    assert action_for(recorded_now).kind == KIND_ANSWER_THEM
    assert recorded_honestly.reply_owed_days == 90
    assert action_for(recorded_honestly).kind == KIND_REVIEW


# --------------------------------------------------------------------------
# The commitment column, which is the only stored plan state
# --------------------------------------------------------------------------

def test_the_commitment_is_what_makes_a_second_run_incremental():
    """`next_follow_up_at` is the one piece of plan state that is written down.

    Everything else is recomputed from the email rows, so this column is what
    stops a rerun from re-deciding the same customers identically.
    """
    action = action_for(thread_with_reply_owed(3), committed_for=NOW + timedelta(days=5))

    # The plan still reasons from the conversation, not from the commitment; the
    # commitment is reported so a stale one is visible rather than silently
    # overwritten.
    assert action.kind == KIND_ANSWER_THEM
    assert action.as_dict()["due_at"]
