"""The customer answered your third email; the first two are unanswered.

That sentence is the whole point of this file. It is the case where a count of
messages gives the wrong answer twice over:

* "3 emails sent, 0 replies" is what the data said, because nothing in the
  application could record a reply at all -- ``EmailStatus.REPLIED`` was
  declared, counted by ``/emails/stats`` as ``reply_rate``, read by every
  "no reply" rule, and impossible to enter.
* Once the reply *is* recorded, "3 sent, 1 replied" is still the wrong frame.
  Messages 1 and 2 were not ignored; the thread moved on. What matters is that
  **they** spoke last and **we** have not answered.

So the model here is a conversation, derived from the outbound rows: who spoke
last, which messages a reply superseded, and which are genuinely awaiting one.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.follow_up_policy import (
    ChurnPolicy,
    FollowUpPolicy,
    assess_churn,
    assess_follow_up,
    derive_thread_state,
)

ROOT = Path(__file__).resolve().parent.parent.parent
NOW = datetime(2026, 10, 16, 12, 0, 0)


def email(*, sent_days_ago=None, replied_days_ago=None, status=None, created=None):
    """One outbound row. ``created_at`` and ``sent_at`` agree, as they do in prod."""
    sent_at = created if created is not None else (
        NOW - timedelta(days=sent_days_ago) if sent_days_ago is not None else None
    )
    replied_at = (
        NOW - timedelta(days=replied_days_ago) if replied_days_ago is not None else None
    )
    return SimpleNamespace(
        status=status or ("replied" if replied_at is not None else "sent"),
        created_at=sent_at,
        sent_at=sent_at,
        replied_at=replied_at,
    )


def the_scenario():
    """#1 and #2 unanswered, the customer answers #3, and we have not replied."""
    return [
        email(sent_days_ago=20),
        email(sent_days_ago=14),
        email(sent_days_ago=8, replied_days_ago=2),
    ]


# --------------------------------------------------------------------------
# The derivation
# --------------------------------------------------------------------------

def test_a_reply_supersedes_the_messages_it_overtook():
    thread = derive_thread_state(the_scenario(), NOW)

    assert thread.outbound == 3
    assert thread.answered is True
    assert thread.superseded == 2, "the two emails the reply overtook"
    assert thread.awaiting_reply == 0, "nothing is waiting on them"
    assert thread.days_since_our_last_email == 8
    assert thread.days_since_their_last_reply == 2


def test_they_wrote_last_is_the_state_the_old_rules_could_not_see():
    thread = derive_thread_state(the_scenario(), NOW)

    assert thread.they_wrote_last is True
    assert thread.reply_owed_days == 2


def test_we_answered_again_so_nothing_is_owed():
    thread = derive_thread_state(
        the_scenario() + [email(sent_days_ago=1)], NOW
    )

    assert thread.answered is True
    assert thread.awaiting_reply == 1, "the email sent after their reply"
    assert thread.superseded == 2
    assert thread.they_wrote_last is False
    assert thread.reply_owed_days is None


def test_no_reply_at_all_means_everything_is_awaiting_one():
    thread = derive_thread_state(
        [email(sent_days_ago=20), email(sent_days_ago=14), email(sent_days_ago=8)], NOW
    )

    assert thread.answered is False
    assert thread.superseded == 0
    assert thread.awaiting_reply == 3
    assert thread.they_wrote_last is False


def test_two_replies_only_supersede_what_came_before_the_first():
    thread = derive_thread_state(
        [
            email(sent_days_ago=20),
            email(sent_days_ago=14, replied_days_ago=10),
            email(sent_days_ago=8, replied_days_ago=2),
        ],
        NOW,
    )

    assert thread.superseded == 1, "only #1 predates the first reply"
    assert thread.awaiting_reply == 0
    assert thread.days_since_their_last_reply == 2


def test_a_reply_timestamp_counts_even_when_the_status_was_not_moved():
    """The timestamp is the fact; the enum is bookkeeping.

    ``PUT /emails/logs/{id}`` cannot set status, and rows edited by hand exist.
    Reading only ``status == 'replied'`` would lose the reply.
    """
    stale = email(sent_days_ago=8, replied_days_ago=2, status="opened")
    thread = derive_thread_state([email(sent_days_ago=20), stale], NOW)

    assert thread.answered is True
    assert thread.they_wrote_last is True


def test_an_email_that_never_arrived_is_not_awaiting_an_answer():
    thread = derive_thread_state(
        [
            email(sent_days_ago=20, status="bounced"),
            email(sent_days_ago=8, status="failed"),
            email(sent_days_ago=3),
        ],
        NOW,
    )

    assert thread.outbound == 1
    assert thread.awaiting_reply == 1


def test_an_empty_history_is_not_an_error():
    thread = derive_thread_state([], NOW)

    assert thread.outbound == 0
    assert thread.answered is False
    assert thread.they_wrote_last is False
    assert thread.days_since_our_last_email is None


# --------------------------------------------------------------------------
# What the rules do with it
# --------------------------------------------------------------------------

def test_churn_stops_calling_it_no_reply_and_names_our_silence():
    thread = derive_thread_state(the_scenario(), NOW)

    result = assess_churn(
        stage="quoting", created_days_ago=30, days_since_contact=2,
        days_in_stage=10, emails_sent=3, emails_replied=1,
        last_email_unopened=False, last_email_days_ago=8, thread=thread,
    )

    assert result.level == "critical"
    assert any("replied 2 day(s) ago" in r for r in result.reasons), result.reasons
    assert not any("received 0 replies" in r for r in result.reasons)
    assert any("Answer their message" in a for a in result.actions)


def test_without_a_thread_the_old_counting_behaviour_is_unchanged():
    """The trap this derivation exists to remove, kept visible.

    Three emails, no reply recorded: critical, "try a different channel". That
    is the correct reading of *that* data. It was the wrong reading of the
    scenario above, which the data could not express.
    """
    result = assess_churn(
        stage="quoting", created_days_ago=30, days_since_contact=8,
        days_in_stage=10, emails_sent=3, emails_replied=0,
        last_email_unopened=False, last_email_days_ago=8,
    )

    assert result.level == "critical"
    assert any("received 0 replies" in r for r in result.reasons)


def test_a_superseded_email_is_not_evidence_the_approach_is_failing():
    """Two overtaken emails plus one awaiting is not a crisis."""
    thread = derive_thread_state(
        [
            email(sent_days_ago=20),                        # overtaken
            email(sent_days_ago=14),                        # overtaken
            email(sent_days_ago=12, replied_days_ago=10),   # the one they answered
            email(sent_days_ago=8),                         # awaiting
        ],
        NOW,
    )
    assert thread.superseded == 2
    assert thread.awaiting_reply == 1

    result = assess_churn(
        stage="contacted", created_days_ago=30, days_since_contact=8,
        days_in_stage=8, emails_sent=4, emails_replied=1,
        last_email_unopened=False, last_email_days_ago=8, thread=thread,
    )

    assert result.level is None, result.reasons


def test_follow_up_says_answer_them_instead_of_no_reply_in_8_days():
    thread = derive_thread_state(the_scenario(), NOW)

    decision = assess_follow_up(
        stage="contacted", days_since_last_email=8, has_any_email=True,
        thread=thread,
    )

    assert decision is not None
    priority, reason, action = decision
    assert priority == "high"
    assert "replied 2 day(s) ago" in reason
    assert "No reply" not in reason
    assert "Answer their message" in action


def test_follow_up_does_not_chase_a_conversation_that_is_answered():
    """We wrote after their reply, so the ball is with them, not us."""
    thread = derive_thread_state(the_scenario() + [email(sent_days_ago=1)], NOW)

    decision = assess_follow_up(
        stage="contacted", days_since_last_email=1, has_any_email=True, thread=thread,
    )

    assert decision is not None
    priority, reason, _ = decision
    assert priority == "low"
    assert "Conversation answered" in reason
    assert "No reply" not in reason


def test_the_unanswered_case_still_produces_the_original_advice():
    thread = derive_thread_state(
        [email(sent_days_ago=8)], NOW
    )
    decision = assess_follow_up(
        stage="contacted", days_since_last_email=8, has_any_email=True, thread=thread,
    )
    assert decision is not None
    assert decision[0] == "high"
    assert "No reply in 8 days" == decision[1]


def test_quoting_still_outranks_everything_except_an_unanswered_customer():
    """`they wrote last` is not a matter of degree; the quoting quirk is kept."""
    quiet = derive_thread_state([email(sent_days_ago=1)], NOW)
    assert assess_follow_up(
        stage="quoting", days_since_last_email=1, has_any_email=True, thread=quiet
    )[0] == "high"

    owed = derive_thread_state(the_scenario(), NOW)
    priority, reason, _ = assess_follow_up(
        stage="quoting", days_since_last_email=8, has_any_email=True, thread=owed
    )
    assert priority == "high"
    assert "have not heard back" in reason, (
        "the reason must describe the outstanding reply, not a quote to chase"
    )


def test_the_reply_owed_threshold_is_tunable_and_named():
    thread = derive_thread_state(the_scenario(), NOW)
    base = dict(
        stage="contacted", created_days_ago=30, days_since_contact=2,
        days_in_stage=2, emails_sent=3, emails_replied=1,
        last_email_unopened=False, last_email_days_ago=8, thread=thread,
    )

    assert assess_churn(**base).level == "critical"
    lenient = assess_churn(**base, policy=ChurnPolicy(reply_owed_critical_after_days=5))
    assert lenient.level == "high"


# --------------------------------------------------------------------------
# The state has to be reachable, and the rules have to be fed
# --------------------------------------------------------------------------

def test_every_production_call_site_passes_the_thread():
    """An optional parameter invites the old bug back silently.

    ``assess_churn`` and ``assess_follow_up`` still accept being called without
    a thread, because the boundary tests exercise one rule at a time. Nothing in
    the application may: without it, the "no reply" rules count every message
    ever sent, which is exactly the behaviour this file exists to replace.
    """
    offenders: list[str] = []
    for base in (ROOT / "backend" / "app",):
        for path in sorted(base.rglob("*.py")):
            if path.name == "follow_up_policy.py" or "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = ast.unparse(node.func).split(".")[-1]
                if name not in ("assess_churn", "assess_follow_up"):
                    continue
                if not any(kw.arg == "thread" for kw in node.keywords):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} {name}(...)"
                    )
    assert not offenders, (
        "these calls judge a customer without the conversation, so a reply to the "
        "third email reads as no reply to any:\n  " + "\n  ".join(offenders)
    )


def test_the_reply_state_has_a_write_path():
    """``EmailStatus.REPLIED`` was unreachable, which made reply_rate a constant.

    Asserted rather than trusted: the enum value, the column and every reader of
    them existed for the whole life of the project while nothing could set them.
    """
    source = (ROOT / "backend" / "app" / "routers" / "emails.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    wrote_status = wrote_timestamp = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            rendered = ast.unparse(target)
            if rendered.endswith(".status") and "REPLIED" in ast.unparse(node.value):
                wrote_status = True
            if rendered.endswith(".replied_at"):
                wrote_timestamp = True

    assert wrote_status, "nothing sets EmailStatus.REPLIED any more"
    assert wrote_timestamp, "nothing sets replied_at any more"
    assert "/logs/{email_id}/reply" in source
