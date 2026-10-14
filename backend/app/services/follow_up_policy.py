"""Follow-up and churn thresholds, in one place and named.

Why this module exists
----------------------
Two functions in ``agent_service`` — ``customer_churn_alerts`` and
``daily_follow_up_intelligence`` — each made a dozen-odd judgement calls using
inline literals::

    if total_sent >= 3 and total_replies == 0:   risk_level = "critical"
    if c.stage in ("interested", "quoting") and days_since > 14:  risk_level = "critical"
    if last_email and (now - last_email.created_at).days >= 3:    priority = "high"

Every one of those is an opinion. Three emails with no reply is not
self-evidently "critical"; fourteen days is not self-evidently the moment a
quote goes cold. Because they were inline, they could not be tested at their
boundaries, could not be tuned without editing control flow, and — the actual
problem — **the two functions disagree with each other**:

* ``customer_churn_alerts`` calls 14 days of silence in the ``quoting`` stage
  ``critical``.
* ``daily_follow_up_intelligence`` calls ``quoting`` ``high`` priority
  *unconditionally*, including a quote sent this morning.

Neither is obviously wrong — "a quote in flight always deserves attention" and
"a quote untouched for two weeks is an emergency" are both defensible. But they
cannot both be the whole rule, and a reader had no way to see the conflict
because it lived in two places.

This module does not resolve that conflict. It makes it **visible and tunable**.
The policies below preserve today's behaviour exactly and are asserted at their
boundaries, so the next person can change a number and see what breaks rather
than discovering it in production.

Not yet done, and named rather than hidden: the agent has its own path to these
signals (``get_customer_signals`` returns the raw numbers and lets the model
judge). These policies remain the deterministic path behind their own endpoints.
Two judgement paths coexisting is a design smell; the resolution is to let the
agent call this policy as a tool so there is one source of thresholds, which is
a smaller change now that the thresholds are in one place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# --- shared vocabulary ------------------------------------------------------

RISK_CRITICAL = "critical"
RISK_HIGH = "high"
RISK_MEDIUM = "medium"

PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"

# Stages that are no longer in play. Never alerted on, never chased.
CLOSED_STAGES = ("completed", "lost")

# A quote in flight. Reused because "still deciding" and "at risk of going cold"
# are different questions asked about the same set of customers.
QUOTING_STAGE = "quoting"
INTERESTED_STAGE = "interested"
CONTACTED_STAGE = "contacted"
SAMPLE_STAGE = "sample"
NEW_STAGE = "new"


@dataclass(frozen=True)
class ChurnPolicy:
    """When a customer counts as at risk, and how badly.

    Ordered from most to least severe: the first rule that fires sets the level.
    """

    # A new customer nobody has touched. The window is short because a lead
    # that was never contacted decays fast.
    untouched_new_after_days: int = 7

    # Emails sent with nothing back. Three is where "maybe busy" becomes
    # "the approach is not working".
    no_reply_critical_emails: int = 3
    no_reply_warning_emails: int = 2

    # Silence since the last contact, by how warm the customer was.
    # A quote or an expressed interest goes cold faster than a first contact.
    silent_warm_after_days: int = 14        # interested / quoting -> critical
    silent_contacted_after_days: int = 21   # contacted -> high
    silent_any_after_days: int = 30         # anything else -> medium

    # Sitting in a stage without moving.
    stuck_quoting_after_days: int = 14
    stuck_sample_after_days: int = 21

    # Quote sent, never opened.
    quote_unopened_after_days: int = 7

    # How many customers get an AI rescue draft, worst first.
    draft_for_top_n: int = 5
    draft_for_levels: tuple[str, ...] = (RISK_CRITICAL, RISK_HIGH)


@dataclass(frozen=True)
class FollowUpPolicy:
    """Who to chase today, and how urgently."""

    # Only customers still in play are considered.
    stages_in_play: tuple[str, ...] = (CONTACTED_STAGE, INTERESTED_STAGE, QUOTING_STAGE)

    # A quote awaiting a decision always deserves attention, however recently it
    # went out. See the module docstring: this is the rule that conflicts with
    # ChurnPolicy.silent_warm_after_days.
    quoting_always_priority: str = PRIORITY_HIGH
    interested_always_priority: str = PRIORITY_MEDIUM

    # Contacted, with a prior email.
    contacted_no_reply_after_days: int = 3   # -> high
    # Contacted, with no email on record at all: the stage was set but nothing
    # was sent, which is a bookkeeping problem rather than a silence problem.
    contacted_without_email_priority: str = PRIORITY_MEDIUM

    draft_for_top_n: int = 5


DEFAULT_CHURN_POLICY = ChurnPolicy()
DEFAULT_FOLLOW_UP_POLICY = FollowUpPolicy()


def days_between(later, earlier) -> Optional[int]:
    """Whole days between two datetimes, or None if either is missing.

    Extracted because both functions computed this inline, and because a missing
    timestamp must read as "unknown", not as zero. The agent found the
    consequence of getting that wrong in real data: every customer with no email
    log looked like ``never_contacted`` even while sitting in the ``quoting``
    stage.
    """
    if later is None or earlier is None:
        return None
    return (later - earlier).days


@dataclass
class ChurnAssessment:
    """The outcome of applying :class:`ChurnPolicy` to one customer."""

    level: Optional[str] = None
    reasons: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    @property
    def at_risk(self) -> bool:
        return self.level is not None

    def escalate(self, level: str) -> None:
        """Raise the level if this is more severe than what we already have."""
        order = {RISK_CRITICAL: 0, RISK_HIGH: 1, RISK_MEDIUM: 2}
        if self.level is None or order[level] < order[self.level]:
            self.level = level


def assess_churn(
    *,
    stage: str,
    created_days_ago: Optional[int],
    days_since_contact: Optional[int],
    days_in_stage: Optional[int],
    emails_sent: int,
    emails_replied: int,
    last_email_unopened: bool,
    last_email_days_ago: Optional[int],
    policy: ChurnPolicy = DEFAULT_CHURN_POLICY,
) -> ChurnAssessment:
    """Decide whether one customer is at risk. Pure; no database, no clock.

    Behaviour is identical to the inline rules this replaced. Every branch is
    asserted at its boundary in ``tests/test_agent_service_policy.py``.
    """
    result = ChurnAssessment()

    # 1. Never contacted, and new.
    if (stage == NEW_STAGE and created_days_ago is not None
            and created_days_ago > policy.untouched_new_after_days):
        result.escalate(RISK_HIGH)
        result.reasons.append(
            f"New customer created {created_days_ago} days ago, never contacted"
        )
        result.actions.append("Send initial outreach email immediately")

    # 2. Emails out, nothing back.
    if emails_sent >= policy.no_reply_critical_emails and emails_replied == 0:
        result.escalate(RISK_CRITICAL)
        result.reasons.append(f"Sent {emails_sent} emails, received 0 replies")
        result.actions.append(
            "Try a different channel (WhatsApp, phone) or change email approach"
        )
    elif emails_sent >= policy.no_reply_warning_emails and emails_replied == 0:
        result.escalate(RISK_HIGH)
        result.reasons.append(f"Sent {emails_sent} emails, no reply yet")
        result.actions.append(
            "Send a follow-up with different angle or value proposition"
        )

    # 3. Silence since the last contact, weighted by how warm they were.
    if days_since_contact is not None:
        if (stage in (INTERESTED_STAGE, QUOTING_STAGE)
                and days_since_contact > policy.silent_warm_after_days):
            result.escalate(RISK_CRITICAL)
            result.reasons.append(
                f"Hot lead gone cold - no contact for {days_since_contact} days "
                f"in {stage} stage"
            )
            result.actions.append(
                "Urgent: Call or WhatsApp immediately, then follow up with email"
            )
        elif (stage == CONTACTED_STAGE
              and days_since_contact > policy.silent_contacted_after_days):
            result.escalate(RISK_HIGH)
            result.reasons.append(
                f"No response for {days_since_contact} days after initial contact"
            )
            result.actions.append(
                "Try re-engagement email with new product offer or discount"
            )
        elif days_since_contact > policy.silent_any_after_days:
            result.escalate(RISK_MEDIUM)
            result.reasons.append(
                f"No contact for {days_since_contact} days - customer may have moved on"
            )
            result.actions.append(
                "Send a check-in email with industry news or new products"
            )

    # 4. Stuck in a stage.
    if (stage == QUOTING_STAGE and days_in_stage is not None
            and days_in_stage > policy.stuck_quoting_after_days):
        result.escalate(RISK_HIGH)
        result.reasons.append("Stuck in quoting stage for over 2 weeks")
        result.actions.append(
            "Follow up on the quote - ask if they need samples or have concerns"
        )
    elif (stage == SAMPLE_STAGE and days_in_stage is not None
          and days_in_stage > policy.stuck_sample_after_days):
        result.escalate(RISK_HIGH)
        result.reasons.append("Sample sent but no feedback for over 3 weeks")
        result.actions.append(
            "Follow up on sample - ask about quality and next steps"
        )

    # 5. A quote that was never even opened.
    if (stage == QUOTING_STAGE and last_email_unopened
            and last_email_days_ago is not None
            and last_email_days_ago > policy.quote_unopened_after_days):
        result.reasons.append("Quote email sent but not opened")
        result.actions.append(
            "Resend with different subject line or try WhatsApp"
        )

    return result


def assess_follow_up(
    *,
    stage: str,
    days_since_last_email: Optional[int],
    has_any_email: bool,
    policy: FollowUpPolicy = DEFAULT_FOLLOW_UP_POLICY,
) -> Optional[tuple[str, str, str]]:
    """Return ``(priority, reason, suggested_action)`` or None if nothing to do.

    Pure. Behaviour matches the inline rules it replaced, including the ordering
    quirk that ``quoting`` and ``interested`` are decided before the silence
    check — so a quote sent today is still ``high``.
    """
    if stage not in policy.stages_in_play:
        return None

    if stage == QUOTING_STAGE:
        return (
            policy.quoting_always_priority,
            "In quoting stage - follow up on quote",
            "Follow up on quoted price, ask if they need samples",
        )

    if stage == INTERESTED_STAGE:
        return (
            policy.interested_always_priority,
            "Customer showed interest - keep momentum",
            "Send product details or case studies",
        )

    if stage == CONTACTED_STAGE:
        if has_any_email and days_since_last_email is not None:
            if days_since_last_email >= policy.contacted_no_reply_after_days:
                return (
                    PRIORITY_HIGH,
                    f"No reply in {days_since_last_email} days",
                    "Send a follow-up email with different angle",
                )
            return (
                PRIORITY_LOW,
                "Recently contacted, wait for reply",
                f"Wait {policy.contacted_no_reply_after_days} more days before follow-up",
            )
        if not has_any_email:
            return (
                policy.contacted_without_email_priority,
                "Marked as contacted but no email record",
                "Send initial outreach email",
            )

    return None
