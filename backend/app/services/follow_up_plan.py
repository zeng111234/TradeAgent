"""The follow-up plan: one commitment per customer, not a report.

Why this module exists
----------------------
``daily_follow_up_intelligence`` answers the question "who looks urgent right
now". Run it every day and it answers the same thing every day. Measured on one
customer with three unanswered emails, three consecutive runs returned
byte-identical entries carrying neither a due date nor a count of how many times
we had already tried. A proactive agent cannot be built on a query whose answer
never changes: it is a loop with no progress, and the person on the receiving end
gets the same email again.

A plan differs from a report in four ways, and each is a field:

* **When.** Every entry has a due time, and the daily job lists what is *due*
  rather than what is overdue-ish. ``Customer.next_follow_up_at`` has existed
  for the whole life of this project and nothing has ever written a value to it:
  the column for this idea was there, the idea was not.
* **How many times.** The attempt count is how many emails have gone out since
  the customer last spoke -- derivable from the thread state, so no new column.
* **When to stop.** Past ``max_attempts`` the plan stops proposing the same kind
  of email and proposes something else. Without a quit rule, "proactive" and
  "harassing" are the same program.
* **What cancels it.** Their reply, a failed delivery, a closed stage.

The schedule is arithmetic and deliberately not a language model's job. The
model's job is the wording and the per-customer judgement, which is what
``get_customer_assessment`` is for; the queue is the part that must be the same
every time it is computed from the same state.

Where the number goes
---------------------
The caller writes ``Customer.next_follow_up_at`` from the planned due time. That
one field is what makes tomorrow's run incremental instead of a rerun: a
commitment made today is not due today, so it does not appear again.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from app.services.follow_up_policy import (
    CONTACTED_STAGE,
    INTERESTED_STAGE,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    QUOTING_STAGE,
    ThreadState,
)

# --- the closed vocabulary of next actions ----------------------------------

#: They spoke last. The only entry whose next action is never in doubt.
KIND_ANSWER_THEM = "answer_them"
#: We spoke last and they have not answered; keep trying by email. Also the
#: entry for a draft that has been sitting unsent.
KIND_SEND_FOLLOW_UP = "send_follow_up"
#: A proposal is written and waiting for a human to approve or reject it.
#: Nothing further is proposed for this customer until that decision is made.
KIND_AWAITING_APPROVAL = "awaiting_approval"
#: Enough emails with no answer. Stop sending the same thing.
KIND_CHANGE_CHANNEL = "change_channel"
#: The last attempt never arrived, so nothing is awaiting a reply.
KIND_FIX_DELIVERY = "fix_delivery"
#: Parked: no further chasing, but not forgotten.
KIND_REVIEW = "review"

#: The closed vocabulary of next actions. Closed on purpose: an open one grows a
#: new synonym every time somebody edits a prompt, and nothing can assert on it.
#: Every kind below is produced by a constructed state in the tests, so none of
#: them is decoration -- an earlier draft carried a "keep_momentum" kind that no
#: reachable state could produce.
ACTION_KINDS = (
    KIND_ANSWER_THEM,
    KIND_SEND_FOLLOW_UP,
    KIND_AWAITING_APPROVAL,
    KIND_CHANGE_CHANNEL,
    KIND_FIX_DELIVERY,
    KIND_REVIEW,
)

#: Kinds that mean "write to them again". The caller uses this to decide whether
#: a draft is wanted, so neither a quit decision nor a pending proposal comes
#: with an email attached -- a proposal to approve a proposal is not a thing.
EMAILING_KINDS = (KIND_SEND_FOLLOW_UP,)


@dataclass(frozen=True)
class FollowUpPlanPolicy:
    """When the next contact is due, and when to stop."""

    #: First chase, this long after our email. Shorter than the churn policy's
    #: silence thresholds on purpose: those decide when a customer is at risk,
    #: this decides when a nudge is welcome.
    first_follow_up_after_days: int = 3

    #: Each further attempt waits this much longer than the one before. Someone
    #: who has ignored two emails does not want the third tomorrow.
    backoff_step_days: int = 3

    #: Unanswered emails after which the plan stops proposing email.
    max_attempts: int = 3

    #: They wrote: answer before this many days have passed. The churn policy
    #: escalates at the same point; here it decides urgency, there it decides risk.
    answer_within_days: int = 2

    #: A parked customer is reviewed this far out: far enough that the plan stops
    #: nagging, near enough that nobody is quietly dropped.
    parked_review_after_days: int = 60

    #: Bounded day. Entries past this are dropped **and counted**, because a
    #: silently truncated list reads as a complete one.
    max_actions_per_day: int = 20

    #: How long a proposal may sit before the human who owns it is reminded.
    #: This is the suspension clock: while a proposal is younger than this the
    #: customer is *parked*, not due, and the scheduled job does not mention
    #: them. Past it, the reminder is about the decision, not about the customer.
    approval_reminder_after_days: int = 2


DEFAULT_FOLLOW_UP_PLAN_POLICY = FollowUpPlanPolicy()

#: Stages the plan will speak about. Anything else is finished business.
PLANNED_STAGES = (CONTACTED_STAGE, INTERESTED_STAGE, QUOTING_STAGE)


@dataclass(frozen=True)
class CustomerPlanInput:
    """Everything the plan needs about one customer. No database, no clock."""

    customer_id: int
    company_name: str
    stage: str
    thread: ThreadState
    #: When the most recent send attempt failed, if it did. A failed attempt is
    #: not an unanswered email, so it must not wait out a follow-up interval.
    last_failed_at: Optional[datetime] = None
    last_failed_reason: str = ""
    has_any_email: bool = True
    contact_email: Optional[str] = None
    contact_name: Optional[str] = None
    #: A commitment already on record, so the plan can report whether it moved.
    committed_for: Optional[datetime] = None


@dataclass
class PlannedAction:
    """One customer's next action, with the time it becomes due."""

    customer_id: int
    company_name: str
    stage: str
    kind: str
    due_at: datetime
    priority: str
    reason: str
    suggested_action: str
    #: Emails sent since the customer last spoke, and still unanswered.
    attempt: int = 0
    contact_email: Optional[str] = None
    contact_name: Optional[str] = None
    evidence: dict = field(default_factory=dict)

    @property
    def emails_them_again(self) -> bool:
        return self.kind in EMAILING_KINDS

    def as_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "company_name": self.company_name,
            "stage": self.stage,
            "kind": self.kind,
            "due_at": self.due_at.isoformat(),
            "priority": self.priority,
            "reason": self.reason,
            "suggested_action": self.suggested_action,
            "attempt": self.attempt,
            "contact_email": self.contact_email,
            "contact_name": self.contact_name,
            "evidence": self.evidence,
        }


@dataclass
class FollowUpPlan:
    """Every customer's next action, plus today's bounded workload.

    Two lists, because they answer two questions and conflating them was a bug:
    ``commitments`` is one entry per customer (what the schedule is, and what the
    caller persists), while ``actions`` is what is *due today*, ordered and
    capped. Persisting only the due ones meant a decision scheduled forward was
    never written down -- so it was recomputed, identically, forever.
    """

    #: One entry per customer with something owed, uncapped and unfiltered.
    commitments: list[PlannedAction] = field(default_factory=list)
    #: Today's list: due entries only when asked, ordered, capped.
    actions: list[PlannedAction] = field(default_factory=list)
    #: Due entries dropped by the cap. Counted so the list is not mistaken for
    #: the whole queue.
    deferred: int = 0

    def __iter__(self):
        return iter(self.actions)

    def __len__(self) -> int:
        return len(self.actions)

    def as_dict(self) -> dict:
        return {
            "count": len(self.actions),
            "commitments": len(self.commitments),
            "deferred": self.deferred,
            "actions": [a.as_dict() for a in self.actions],
        }


def _today(now: datetime) -> datetime:
    """Midnight of the current day.

    Used wherever an action is due *immediately*. ``12:14:15.271370`` is false
    precision for a daily plan, and it made two reads of the same state differ --
    the sort of thing that turns an idempotent job into a diffing exercise.
    """
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _due_for_follow_up(
    last_outbound_at: Optional[datetime],
    attempt: int,
    now: datetime,
    policy: FollowUpPlanPolicy,
) -> datetime:
    """When the next email is due: base interval plus a step per attempt."""
    if last_outbound_at is None:
        return _today(now)
    interval = policy.first_follow_up_after_days + policy.backoff_step_days * max(0, attempt - 1)
    return last_outbound_at + timedelta(days=interval)


def plan_one(
    state: CustomerPlanInput,
    *,
    now: datetime,
    policy: FollowUpPlanPolicy = DEFAULT_FOLLOW_UP_PLAN_POLICY,
) -> Optional[PlannedAction]:
    """The next action for one customer, or None if nothing is owed.

    Precedence is the order of the checks, and it is a statement about what
    matters: an unanswered customer outranks a broken address, which outranks
    another email, which outranks keeping momentum.
    """
    if state.stage not in PLANNED_STAGES:
        return None

    thread = state.thread
    base = dict(
        customer_id=state.customer_id,
        company_name=state.company_name,
        stage=state.stage,
        contact_email=state.contact_email,
        contact_name=state.contact_name,
    )

    def evidence(extra: dict) -> dict:
        return {
            "awaiting_reply": thread.awaiting_reply,
            "superseded": thread.superseded,
            "answered": thread.answered,
            "they_wrote_last": thread.they_wrote_last,
            "days_since_our_last_email": thread.days_since_our_last_email,
            "days_since_their_last_reply": thread.days_since_their_last_reply,
            "pending_proposals": thread.pending_proposals,
            "pending_proposal_days": thread.pending_proposal_days,
            "rejected_proposals": thread.rejected_proposals,
            **extra,
        }

    # 0. A proposal is written and waiting for a human. The state is *suspended*:
    #    nothing further is proposed for this customer, and the entry is not due
    #    until the decision has been outstanding long enough to remind anyone.
    #    Checked before everything, including "they wrote last" -- the answer to
    #    their message may well be the draft that is already waiting.
    if thread.pending_proposals:
        waiting = thread.pending_proposal_days or 0
        stale = waiting >= policy.approval_reminder_after_days
        reason = (
            f"{thread.pending_proposals} draft(s) waiting for your approval "
            f"(oldest {waiting} day(s))"
        )
        if thread.they_wrote_last:
            reason += f"; they replied {thread.reply_owed_days} day(s) ago"
        return PlannedAction(
            **base,
            kind=KIND_AWAITING_APPROVAL,
            # A row with no timestamp is bad data; treating it as written today
            # is better than refusing to plan for the customer at all.
            due_at=(thread.oldest_pending_at or _today(now)) + timedelta(
                days=policy.approval_reminder_after_days
            ),
            # Someone is waiting on the other end, so it matters -- but the next
            # move is a person's, and the plan says that instead of proposing
            # more outreach on top of an unanswered proposal.
            priority=PRIORITY_HIGH if (thread.they_wrote_last or stale) else PRIORITY_MEDIUM,
            reason=reason,
            suggested_action="Review the draft: send it, edit it, or reject it",
            attempt=thread.awaiting_reply,
            evidence=evidence({"approval_reminder_after_days":
                               policy.approval_reminder_after_days}),
        )

    # 1. They spoke last. Due the moment they wrote, so it is overdue immediately.
    if thread.they_wrote_last and thread.last_reply_at is not None:
        owed = thread.reply_owed_days or 0
        late = owed >= policy.answer_within_days
        return PlannedAction(
            **base,
            kind=KIND_ANSWER_THEM,
            due_at=thread.last_reply_at,
            priority=PRIORITY_HIGH,
            reason=(
                f"They replied {owed} day(s) ago and have not heard back"
                + (" - past the window for a reply" if late else "")
            ),
            suggested_action="Answer their message - they wrote last",
            attempt=0,
            evidence=evidence({"reply_owed_days": owed}),
        )

    # 2. The last attempt never arrived, so nothing is awaiting a reply.
    if state.last_failed_at is not None:
        return PlannedAction(
            **base,
            kind=KIND_FIX_DELIVERY,
            due_at=_today(now),
            priority=PRIORITY_HIGH,
            reason=(
                "The last email did not go out"
                + (f" ({state.last_failed_reason})" if state.last_failed_reason else "")
            ),
            suggested_action="Check the address, then resend or use another channel",
            attempt=thread.awaiting_reply,
            evidence=evidence({"last_failed_at": state.last_failed_at.isoformat()}),
        )

    attempt = thread.awaiting_reply

    # 3. Nothing on record behind this stage. Bookkeeping, not silence -- and the
    #    reason has to name the stage it is actually in. Saying "marked as
    #    contacted" about a quoting customer is a small lie in a field a human
    #    reads.
    if attempt == 0 and not state.has_any_email and not thread.answered:
        if state.stage == CONTACTED_STAGE:
            reason = "Marked as contacted but no email record"
        else:
            reason = f"In {state.stage} stage but no email on record"
        return PlannedAction(
            **base,
            kind=KIND_SEND_FOLLOW_UP,
            due_at=_today(now),
            priority=PRIORITY_MEDIUM,
            reason=reason,
            suggested_action="Send the first email for this customer",
            attempt=0,
            evidence=evidence({"has_any_email": False}),
        )

    # 4. Enough emails with no answer. Stop sending the same thing -- this is the
    #    rule that separates a proactive agent from a machine that nags.
    if attempt >= policy.max_attempts:
        parked_at = _due_for_follow_up(
            thread.last_outbound_at, attempt, now, policy
        )
        return PlannedAction(
            **base,
            kind=KIND_CHANGE_CHANNEL if attempt == policy.max_attempts else KIND_REVIEW,
            due_at=(
                parked_at
                if attempt == policy.max_attempts
                else parked_at + timedelta(days=policy.parked_review_after_days)
            ),
            priority=PRIORITY_MEDIUM if attempt == policy.max_attempts else PRIORITY_LOW,
            reason=(
                f"{attempt} emails sent, none answered"
                + (
                    " - another email is unlikely to work"
                    if attempt == policy.max_attempts
                    else " - no further email planned; parked for review"
                )
            ),
            suggested_action=(
                "Try a different channel (WhatsApp, phone) or a different angle"
                if attempt == policy.max_attempts
                else "Park this customer and review later"
            ),
            attempt=attempt,
            evidence=evidence({"max_attempts": policy.max_attempts}),
        )

    # 5. We spoke last and they have not answered. One more email, when due.
    if attempt >= 1 or thread.answered:
        due = _due_for_follow_up(thread.last_outbound_at, max(1, attempt), now, policy)
        priority = (
            PRIORITY_HIGH if state.stage in (CONTACTED_STAGE, QUOTING_STAGE)
            else PRIORITY_MEDIUM
        )
        return PlannedAction(
            **base,
            kind=KIND_SEND_FOLLOW_UP,
            due_at=due,
            priority=priority,
            reason=(
                f"No answer to {attempt} email(s)"
                if attempt
                else "They replied earlier; the conversation is still open"
            ),
            suggested_action="Send a follow-up with a different angle",
            attempt=attempt,
            evidence=evidence({"due_interval_days": policy.first_follow_up_after_days
                               + policy.backoff_step_days * max(0, attempt - 1)}),
        )

    # 6. Rows exist but none of them went out, and nothing is pending: the only
    #    proposals on record were rejected. Say so, because "nothing has been
    #    sent yet" and "your last three attempts were turned down" are different
    #    situations and only one of them wants the same email again.
    if state.has_any_email and thread.outbound == 0:
        if thread.rejected_proposals:
            reason = (
                f"{thread.rejected_proposals} proposal(s) rejected; nothing has "
                "been sent to this customer"
            )
            action = "Write a different approach, or set the stage back"
        else:
            reason = "Nothing has been sent to this customer yet"
            action = "Send the first email for this customer"
        return PlannedAction(
            **base,
            kind=KIND_SEND_FOLLOW_UP,
            due_at=_today(now),
            priority=PRIORITY_HIGH if state.stage == QUOTING_STAGE else PRIORITY_MEDIUM,
            reason=reason,
            suggested_action=action,
            attempt=0,
            evidence=evidence({"outbound": 0}),
        )

    return None


def plan_follow_ups(
    states: list[CustomerPlanInput],
    *,
    now: datetime,
    due_only: bool = False,
    policy: FollowUpPlanPolicy = DEFAULT_FOLLOW_UP_PLAN_POLICY,
) -> FollowUpPlan:
    """The plan for a whole book of customers, ordered and bounded.

    ``due_only`` shapes today's list; it does **not** shape the commitments. That
    distinction is load-bearing: filtering first and persisting the result meant
    the entries scheduled forward -- the ones that make tomorrow incremental --
    were never written down.
    """
    commitments = [
        action
        for state in states
        if (action := plan_one(state, now=now, policy=policy)) is not None
    ]

    # Most overdue first, then by priority, then stable by customer, so two runs
    # over the same state produce the same order.
    ordered = sorted(
        commitments,
        key=lambda a: (a.due_at, {"high": 0, "medium": 1, "low": 2}.get(a.priority, 3),
                       a.customer_id),
    )

    visible = [a for a in ordered if a.due_at <= now] if due_only else ordered
    deferred = max(0, len(visible) - policy.max_actions_per_day)

    return FollowUpPlan(
        commitments=ordered,
        actions=visible[: policy.max_actions_per_day],
        deferred=deferred,
    )
