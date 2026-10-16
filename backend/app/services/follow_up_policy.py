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
from datetime import datetime
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

    # They wrote and we have not answered. Not churn -- *our* silence -- but the
    # customer is at risk either way, and it is the one case where the next
    # action is unambiguous.
    reply_owed_critical_after_days: int = 2

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


# --- the conversation, not the message --------------------------------------

#: Statuses that mean "this email left the building".
SENT_STATES = ("sent", "delivered", "opened", "replied")


@dataclass(frozen=True)
class ThreadState:
    """Who spoke last, and whether anyone owes anyone an answer.

    A reply is an event on a *conversation*, not a property of one message. Ask
    "was email 2 answered" and a customer who answers your third email looks
    like they ignored the first two. Ask "who spoke last" and the same data
    reads correctly.

    Three consequences, and all three were wrong before:

    * Emails sent **before** the first reply are *superseded*, not ignored. The
      thread moved on. Chasing someone about message 1 after they answered
      message 3 is noise.
    * Emails sent **after** the last reply are the ones genuinely *awaiting* an
      answer. That is the number a "no reply" rule should count.
    * When the newest thing in the thread is *their* reply, **we** are the silent
      party. The old rules could not see this at all: they counted outbound rows
      and outbound timestamps, so a customer who wrote back yesterday read as
      "3 emails sent, 0 replies received".

    Derived from the outbound rows only, because there are no inbound rows to
    read: ``EmailLog`` records what this system sent.
    """

    outbound: int = 0
    superseded: int = 0
    awaiting_reply: int = 0
    answered: bool = False
    first_reply_at: Optional[datetime] = None
    last_reply_at: Optional[datetime] = None
    last_outbound_at: Optional[datetime] = None
    days_since_our_last_email: Optional[int] = None
    days_since_their_last_reply: Optional[int] = None
    #: True when their reply is newer than everything we sent: we owe them one.
    they_wrote_last: bool = False
    #: How long we have owed it. None when we do not.
    reply_owed_days: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "outbound": self.outbound,
            "answered": self.answered,
            "superseded": self.superseded,
            "awaiting_reply": self.awaiting_reply,
            "they_wrote_last": self.they_wrote_last,
            "reply_owed_days": self.reply_owed_days,
            "days_since_our_last_email": self.days_since_our_last_email,
            "days_since_their_last_reply": self.days_since_their_last_reply,
            "first_reply_at": self.first_reply_at.isoformat() if self.first_reply_at else None,
            "last_reply_at": self.last_reply_at.isoformat() if self.last_reply_at else None,
        }


@dataclass(frozen=True)
class ThreadPolicy:
    """How urgent an unanswered message from the customer is."""

    # They wrote and we have not. Unlike silence, this delay is unambiguously
    # ours, so it starts high and gets worse fast.
    reply_owed_priority: str = PRIORITY_HIGH
    # Two days is past "we will get to it". A buyer who wrote and heard nothing
    # is worse off than one who was never contacted, because they are waiting.
    reply_owed_critical_after_days: int = 2


DEFAULT_THREAD_POLICY = ThreadPolicy()


def derive_thread_state(emails, now: datetime) -> ThreadState:
    """Read one conversation out of the outbound rows. Pure; no database.

    ``emails`` is any iterable of objects with ``status``, ``created_at``,
    ``sent_at`` and ``replied_at``. A row counts as answered if ``replied_at``
    is set, whether or not ``status`` was moved to match: the timestamp is the
    fact, the enum is bookkeeping, and inconsistent rows exist in practice.
    """
    rows = [e for e in emails if e is not None]
    outbound = [e for e in rows if _status_of(e) in SENT_STATES]

    def when(email) -> Optional[datetime]:
        return getattr(email, "sent_at", None) or getattr(email, "created_at", None)

    stamped = [
        when(e) for e in outbound
        if when(e) is not None
    ]
    last_outbound_at = max(stamped) if stamped else None

    replies = sorted(
        e.replied_at for e in rows if getattr(e, "replied_at", None) is not None
    )
    first_reply_at = replies[0] if replies else None
    last_reply_at = replies[-1] if replies else None

    superseded = 0
    awaiting = 0
    for email in outbound:
        sent_at = when(email)
        answered_itself = getattr(email, "replied_at", None) is not None
        if first_reply_at is not None and sent_at is not None and sent_at < first_reply_at:
            # Sent before the conversation was answered. If it carries its own
            # reply it was that reply; otherwise the thread overtook it.
            if not answered_itself:
                superseded += 1
            continue
        if last_reply_at is not None and sent_at is not None and sent_at <= last_reply_at:
            # It is the message they answered (or an earlier one).
            if not answered_itself:
                superseded += 1
            continue
        awaiting += 1

    they_wrote_last = (
        last_reply_at is not None
        and (last_outbound_at is None or last_reply_at > last_outbound_at)
    )

    return ThreadState(
        outbound=len(outbound),
        superseded=superseded,
        awaiting_reply=awaiting,
        answered=bool(replies),
        first_reply_at=first_reply_at,
        last_reply_at=last_reply_at,
        last_outbound_at=last_outbound_at,
        days_since_our_last_email=days_between(now, last_outbound_at),
        days_since_their_last_reply=days_between(now, last_reply_at),
        they_wrote_last=they_wrote_last,
        reply_owed_days=days_between(now, last_reply_at) if they_wrote_last else None,
    )


def _status_of(email) -> str:
    value = getattr(email, "status", "")
    return value.value if hasattr(value, "value") else str(value or "")



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
    thread: Optional[ThreadState] = None,
    thread_policy: ThreadPolicy = DEFAULT_THREAD_POLICY,
    policy: ChurnPolicy = DEFAULT_CHURN_POLICY,
) -> ChurnAssessment:
    """Decide whether one customer is at risk. Pure; no database, no clock.

    Every branch is asserted at its boundary in
    ``tests/test_agent_service_policy.py``.

    Pass ``thread`` (from :func:`derive_thread_state`) to judge the customer by
    the *conversation* rather than by a count of messages. Without it the
    "no reply" rules count every email ever sent, so a customer who answered the
    third one is reported as having received no reply to any of them. Every
    caller in this codebase passes it -- asserted by
    ``tests/test_thread_state.py`` -- and the parameter stays optional only so
    the boundary tests can exercise one rule at a time.
    """
    result = ChurnAssessment()

    # 0. They wrote and we have not answered. Checked first because it is the
    # only case where the reason and the action are not in doubt, and because it
    # is invisible to every other rule here: those count outbound rows.
    if thread is not None and thread.they_wrote_last:
        owed = thread.reply_owed_days or 0
        if owed >= policy.reply_owed_critical_after_days:
            result.escalate(RISK_CRITICAL)
        else:
            result.escalate(RISK_HIGH)
        result.reasons.append(
            f"Customer replied {owed} day(s) ago and has not heard back"
        )
        result.actions.append(
            "Answer their message. They wrote last, so the delay is ours."
        )

    # 1. Never contacted, and new.
    if (stage == NEW_STAGE and created_days_ago is not None
            and created_days_ago > policy.untouched_new_after_days):
        result.escalate(RISK_HIGH)
        result.reasons.append(
            f"New customer created {created_days_ago} days ago, never contacted"
        )
        result.actions.append("Send initial outreach email immediately")

    # 2. Emails out, nothing back.
    #
    # The count is of emails still *awaiting* a reply, not of emails ever sent.
    # With a thread, two messages the customer overtook by answering the third
    # are not evidence that the approach is failing.
    unreplied = thread.awaiting_reply if thread is not None else emails_sent
    ever_answered = thread.answered if thread is not None else emails_replied > 0
    if unreplied >= policy.no_reply_critical_emails and not ever_answered:
        result.escalate(RISK_CRITICAL)
        result.reasons.append(f"Sent {unreplied} emails, received 0 replies")
        result.actions.append(
            "Try a different channel (WhatsApp, phone) or change email approach"
        )
    elif unreplied >= policy.no_reply_warning_emails and not ever_answered:
        result.escalate(RISK_HIGH)
        result.reasons.append(f"Sent {unreplied} emails, no reply yet")
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
    thread: Optional[ThreadState] = None,
    thread_policy: ThreadPolicy = DEFAULT_THREAD_POLICY,
    policy: FollowUpPolicy = DEFAULT_FOLLOW_UP_POLICY,
) -> Optional[tuple[str, str, str]]:
    """Return ``(priority, reason, suggested_action)`` or None if nothing to do.

    Pure. The ordering quirk is preserved for everything except the one case
    that is not a judgement call: ``quoting`` and ``interested`` are still
    decided before the silence check, so a quote sent today is still ``high``.

    "They wrote last" is checked *before* those, because it is not a matter of
    degree. A customer who replies and hears nothing is the one thing on this
    list where the next action is unambiguous, and it used to be read as the
    exact opposite -- "No reply in 8 days", suggesting a follow-up with a
    different angle, to a customer who had replied two days earlier.
    """
    if thread is not None and thread.they_wrote_last:
        owed = thread.reply_owed_days or 0
        return (
            thread_policy.reply_owed_priority,
            f"They replied {owed} day(s) ago and have not heard back",
            "Answer their message - they wrote last",
        )

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
        if thread is not None and thread.answered:
            # They have answered at some point and it is not us who owes one.
            # Whatever this needs, it is not "no reply in N days".
            return (
                PRIORITY_LOW,
                f"Conversation answered {thread.days_since_their_last_reply} "
                f"day(s) ago; waiting on them",
                "Wait for their decision. Do not chase as if unanswered.",
            )
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
