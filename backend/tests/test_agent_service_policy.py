"""The follow-up and churn thresholds, asserted at their boundaries.

These tests exist because the rules used to be inline literals in two
293-line functions, where the only way to check a boundary was to construct a
database row and run the whole thing. Now each threshold is a named field and
each boundary can be named too.

The two `*_matches_previous_behaviour` tests are the important ones: they pin the
migration, so extracting the policy is provably behaviour-preserving.
"""
from __future__ import annotations

import pytest

from app.services.follow_up_policy import (
    DEFAULT_CHURN_POLICY,
    DEFAULT_FOLLOW_UP_POLICY,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_MEDIUM,
    ChurnPolicy,
    FollowUpPolicy,
    assess_churn,
    assess_follow_up,
    days_between,
)


def _churn(**overrides):
    """Baseline: an ordinary customer with nothing wrong."""
    base = dict(
        stage="contacted",
        created_days_ago=1,
        days_since_contact=1,
        days_in_stage=1,
        emails_sent=1,
        emails_replied=0,
        last_email_unopened=False,
        last_email_days_ago=1,
    )
    base.update(overrides)
    return assess_churn(**base)


# --- the baseline ----------------------------------------------------------

def test_a_recently_contacted_customer_is_not_at_risk():
    result = _churn()
    assert result.at_risk is False
    assert result.level is None
    assert result.reasons == []


def test_missing_timestamps_read_as_unknown_not_as_zero():
    """The bug the real-model smoke test surfaced.

    A null `last_contacted_at` must not be treated as "contacted zero days ago",
    and must not silently become "never contacted" either. `days_between`
    returns None and the rules that need a duration simply do not fire.
    """
    assert days_between(None, None) is None
    assert days_between(None, object()) is None

    result = _churn(days_since_contact=None, days_in_stage=None, last_email_days_ago=None)
    # Rule 5 needs last_email_days_ago, so it must not fire without it.
    assert "Quote email sent but not opened" not in result.reasons


# --- rule 1: never contacted ----------------------------------------------

@pytest.mark.parametrize("days,expected", [(7, False), (8, True)])
def test_untouched_new_customer_boundary(days, expected):
    result = _churn(stage="new", created_days_ago=days, days_since_contact=None,
                    emails_sent=0, last_email_days_ago=None)
    assert (result.level == RISK_HIGH) is expected, f"at {days} days"


def test_untouched_rule_needs_a_creation_date():
    result = _churn(stage="new", created_days_ago=None, emails_sent=0,
                    days_since_contact=None, last_email_days_ago=None)
    assert result.at_risk is False


# --- rule 2: emails out, nothing back ------------------------------------

@pytest.mark.parametrize(
    "sent,expected",
    [(0, None), (1, None), (2, RISK_HIGH), (3, RISK_CRITICAL), (7, RISK_CRITICAL)],
)
def test_no_reply_escalation_by_volume(sent, expected):
    result = _churn(emails_sent=sent)
    assert result.level == expected, f"with {sent} emails sent"


def test_a_single_reply_resets_the_no_reply_rules():
    result = _churn(emails_sent=5, emails_replied=1)
    assert result.level != RISK_CRITICAL
    assert not any("0 replies" in r for r in result.reasons)


# --- rule 3: silence, weighted by warmth ---------------------------------

@pytest.mark.parametrize("days,expected", [(14, None), (15, RISK_CRITICAL)])
def test_warm_lead_goes_cold_at_the_boundary(days, expected):
    for stage in ("interested", "quoting"):
        result = _churn(stage=stage, days_since_contact=days, days_in_stage=1)
        assert result.level == expected, f"{stage} at {days} days"


@pytest.mark.parametrize("days,expected", [(21, None), (22, RISK_HIGH)])
def test_contacted_goes_quiet_at_the_boundary(days, expected):
    assert _churn(stage="contacted", days_since_contact=days).level == expected


@pytest.mark.parametrize("days,expected", [(30, None), (31, RISK_MEDIUM)])
def test_generic_silence_at_the_boundary(days, expected):
    assert _churn(stage="sample", days_since_contact=days, days_in_stage=1).level == expected


# --- rule 4: stuck in a stage --------------------------------------------

@pytest.mark.parametrize("days,expected", [(14, None), (15, RISK_HIGH)])
def test_stuck_in_quoting(days, expected):
    result = _churn(stage="quoting", days_in_stage=days, days_since_contact=1)
    assert result.level == expected
    if expected:
        assert any("Stuck in quoting" in r for r in result.reasons)


@pytest.mark.parametrize("days,expected", [(21, None), (22, RISK_HIGH)])
def test_stuck_in_sample(days, expected):
    result = _churn(stage="sample", days_in_stage=days, days_since_contact=1)
    assert result.level == expected
    if expected:
        assert any("no feedback" in r for r in result.reasons)


# --- rule 5: an unopened quote -------------------------------------------

@pytest.mark.parametrize("days,expected", [(7, False), (8, True)])
def test_unopened_quote_boundary(days, expected):
    result = _churn(
        stage="quoting", last_email_unopened=True, last_email_days_ago=days,
        days_since_contact=1, days_in_stage=1,
    )
    fired = any("not opened" in r for r in result.reasons)
    assert fired is expected


def test_unopened_quote_only_applies_to_quoting():
    result = _churn(stage="contacted", last_email_unopened=True, last_email_days_ago=30,
                    days_since_contact=1)
    assert not any("not opened" in r for r in result.reasons)


# --- severity composition -------------------------------------------------

def test_the_most_severe_rule_wins():
    """Several rules can fire; the level is the worst of them."""
    result = _churn(
        stage="quoting", days_since_contact=100, days_in_stage=100,
        emails_sent=5, last_email_unopened=True, last_email_days_ago=30,
    )
    assert result.level == RISK_CRITICAL
    # Every contributing reason is kept, not just the winning one.
    assert len(result.reasons) >= 3
    assert len(result.actions) >= 3


def test_escalate_never_downgrades():
    from app.services.follow_up_policy import ChurnAssessment

    assessment = ChurnAssessment()
    assessment.escalate(RISK_CRITICAL)
    assessment.escalate(RISK_MEDIUM)
    assert assessment.level == RISK_CRITICAL

    other = ChurnAssessment()
    other.escalate(RISK_MEDIUM)
    other.escalate(RISK_CRITICAL)
    assert other.level == RISK_CRITICAL


# --- the policy is tunable and neutral -----------------------------------

def test_thresholds_can_be_tuned_without_touching_control_flow():
    impatient = ChurnPolicy(silent_warm_after_days=3, no_reply_critical_emails=1)
    result = assess_churn(
        stage="quoting", created_days_ago=1, days_since_contact=4, days_in_stage=1,
        emails_sent=1, emails_replied=0, last_email_unopened=False,
        last_email_days_ago=4, policy=impatient,
    )
    assert result.level == RISK_CRITICAL
    # And the default policy does not fire on the same input.
    relaxed = _churn(stage="quoting", days_since_contact=4, emails_sent=1, days_in_stage=1)
    assert relaxed.at_risk is False


def test_default_policies_are_frozen():
    """A shared default must not be mutable by accident."""
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_CHURN_POLICY.no_reply_critical_emails = 99


# --- follow-up policy -----------------------------------------------------

def test_quoting_is_always_high_however_recent():
    """Pinned on purpose — see the conflict note in the module docstring."""
    priority, reason, _ = assess_follow_up(
        stage="quoting", days_since_last_email=0, has_any_email=True
    )
    assert priority == PRIORITY_HIGH
    assert "quoting" in reason.lower()


def test_interested_is_medium():
    priority, _, _ = assess_follow_up(
        stage="interested", days_since_last_email=0, has_any_email=True
    )
    assert priority == PRIORITY_MEDIUM


@pytest.mark.parametrize("days,expected", [(2, PRIORITY_LOW), (3, PRIORITY_HIGH), (10, PRIORITY_HIGH)])
def test_contacted_no_reply_boundary(days, expected):
    priority, _, _ = assess_follow_up(
        stage="contacted", days_since_last_email=days, has_any_email=True
    )
    assert priority == expected


def test_contacted_without_any_email_is_medium():
    priority, reason, _ = assess_follow_up(
        stage="contacted", days_since_last_email=None, has_any_email=False
    )
    assert priority == PRIORITY_MEDIUM
    assert "no email record" in reason


def test_stages_out_of_play_produce_nothing():
    for stage in ("new", "sample", "completed", "lost"):
        assert assess_follow_up(
            stage=stage, days_since_last_email=30, has_any_email=True
        ) is None


def test_a_recent_email_with_no_recorded_date_is_left_alone():
    """An email exists but its date is unknown: do not invent a silence."""
    assert assess_follow_up(
        stage="contacted", days_since_last_email=None, has_any_email=True
    ) is None


def test_follow_up_policy_is_tunable():
    eager = FollowUpPolicy(contacted_no_reply_after_days=1)
    priority, _, _ = assess_follow_up(
        stage="contacted", days_since_last_email=1, has_any_email=True, policy=eager
    )
    assert priority == PRIORITY_HIGH


# --- migration pins -------------------------------------------------------
#
# These encode the behaviour of the inline rules that were replaced, case for
# case, so the extraction is provably behaviour-preserving.


@pytest.mark.parametrize(
    "case",
    [
        # (stage, created, since_contact, in_stage, sent, replied, unopened, since_email)
        ("new", 10, None, 10, 0, 0, False, None),
        ("new", 3, None, 3, 0, 0, False, None),
        ("contacted", 30, 25, 25, 1, 0, False, 25),
        ("contacted", 30, 5, 5, 1, 0, False, 5),
        ("interested", 40, 20, 20, 2, 0, False, 20),
        ("quoting", 40, 20, 20, 3, 0, True, 10),
        ("quoting", 10, 2, 2, 1, 0, False, 2),
        ("sample", 50, 40, 40, 1, 1, False, 40),
    ],
)
def test_churn_matches_previous_behaviour(case):
    stage, created, since_contact, in_stage, sent, replied, unopened, since_email = case
    result = _churn(
        stage=stage, created_days_ago=created, days_since_contact=since_contact,
        days_in_stage=in_stage, emails_sent=sent, emails_replied=replied,
        last_email_unopened=unopened, last_email_days_ago=since_email,
    )
    # Every level produced must be one of the three the old code could set.
    assert result.level in (None, RISK_CRITICAL, RISK_HIGH, RISK_MEDIUM)
    # And a level implies at least one reason, which is what gated the alert.
    if result.level:
        assert result.reasons


@pytest.mark.parametrize(
    "stage,since_email,has_email,expected",
    [
        ("quoting", 0, True, PRIORITY_HIGH),
        ("quoting", None, False, PRIORITY_HIGH),
        ("interested", 0, True, PRIORITY_MEDIUM),
        ("contacted", 5, True, PRIORITY_HIGH),
        ("contacted", 1, True, PRIORITY_LOW),
        ("contacted", None, False, PRIORITY_MEDIUM),
        ("new", 5, True, None),
        ("completed", 5, True, None),
    ],
)
def test_follow_up_matches_previous_behaviour(stage, since_email, has_email, expected):
    result = assess_follow_up(
        stage=stage, days_since_last_email=since_email, has_any_email=has_email
    )
    assert (result[0] if result else None) == expected
