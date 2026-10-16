"""What happened to this customer, and why.

Debugging a failed customer handling starts from the customer -- "Martin got
nothing this week", "this draft was never sent", "why is this account parked?" --
and before this module there was no way to make that trip. The facts existed in
four places that did not reference each other:

* ``email_logs`` held the messages and their failures;
* the follow-up plan computed a decision for every customer, but you could only
  see it by asking for the whole book (and the POST version writes commitments,
  so *inspecting* the plan changed it);
* ``agent_runs`` held traces, but carried **no customer reference at all** --
  only a free-text goal and a JSON trace, so a run could not be found from the
  customer it was about;
* the thread state (the numbers every rule actually reads) was derived on the
  fly and never shown.

This assembles those into one answer, and adds the part that makes it a
diagnosis rather than a data dump: a short list of findings in plain language,
most likely cause first.

It is **read-only**. Every query here is a SELECT and the plan is built with
``persist=False``. A debug view that changes the thing you are debugging is worse
than no debug view, because you cannot look twice.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.customer import Customer, CustomerContact, CustomerNote
from app.models.email_log import EmailLog
from app.services.follow_up_policy import (
    DEFAULT_THREAD_POLICY,
    assess_churn,
    days_between,
    derive_thread_state,
)

logger = logging.getLogger(__name__)

MAX_EMAILS = 50
#: How many recent runs to search for mentions of this customer. The trace is a
#: JSON blob with no index on its contents, so this is a scan; bounded, and the
#: answer says when it was bounded rather than pretending to be complete.
MAX_RUNS_SCANNED = 300

SENT_STATES = ("sent", "delivered", "opened", "replied")


def _status(value) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def tool_calls_naming(trace_json: Optional[str], customer_id: int) -> list[dict]:
    """The tool calls in this run's trace that mention this customer.

    Matched on the parsed argument, not on a substring of the JSON: ``"4"``
    appears inside ``"14"`` and ``"40"``, and a debug view that shows another
    customer's run is worse than showing none.
    """
    if not trace_json:
        return []
    try:
        steps = json.loads(trace_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(steps, list):
        return []

    naming: list[dict] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        for call in step.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            arguments = call.get("arguments") or {}
            if not isinstance(arguments, dict):
                continue
            if str(arguments.get("customer_id", "")) == str(customer_id):
                naming.append({
                    "round": step.get("round"),
                    "tool": call.get("name"),
                    "arguments": arguments,
                    "ok": call.get("ok"),
                })
    return naming


async def runs_for_customer(
    db: AsyncSession, customer: Customer, *, limit: int = MAX_RUNS_SCANNED
) -> dict:
    """The agent runs that touched this customer, newest first.

    Two sources, and the difference between them matters:

    * **The column.** ``agent_runs.customer_id`` (migration
      ``0001_agent_runs_customer_id``) is exact and indexed. A run started with
      ``customer_id`` is found here in one query, whatever its trace says.
    * **The trace.** Rows written before the column existed have no value to
      read, so they are still found by scanning recent traces for a tool call
      that named this customer -- the old way, kept because those rows are real
      history and dropping them would make the view lie by omission.

    The scan is bounded and only runs when there are rows the column cannot
    answer for, so on a database that has been running since the migration the
    common case costs one indexed query.
    """
    by_column = (
        await db.execute(
            select(AgentRun)
            .where(AgentRun.customer_id == customer.id)
            .order_by(AgentRun.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    legacy_rows = (
        await db.execute(
            select(AgentRun)
            .where(AgentRun.customer_id.is_(None))
            .order_by(AgentRun.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    matches = []
    seen: set[str] = set()
    for run in by_column:
        seen.add(run.run_id)
        matches.append(_run_payload(run, customer, matched_by="customer_id column"))
    for run in legacy_rows:
        calls = tool_calls_naming(run.trace, customer.id)
        mentioned_in_goal = (
            str(customer.id) in (run.goal or "")
            or (customer.company_name or "")[:20] in (run.goal or "")
        )
        if not calls and not mentioned_in_goal:
            continue
        matches.append(
            _run_payload(
                run, customer,
                matched_by="tool arguments" if calls else "goal text",
                calls=calls,
            )
        )

    matches.sort(key=lambda entry: entry["created_at"] or "", reverse=True)
    return {
        "runs": matches,
        "runs_scanned": len(by_column) + len(legacy_rows),
        "scan_truncated": len(by_column) >= limit or len(legacy_rows) >= limit,
        "legacy_rows_searched": len(legacy_rows),
    }


def _run_payload(run: AgentRun, customer: Customer, *, matched_by: str,
                 calls: list[dict] | None = None) -> dict:
    calls = tool_calls_naming(run.trace, customer.id) if calls is None else calls
    return {
        "run_id": run.run_id,
        "status": run.status,
        "stop_reason": run.stop_reason,
        "error": run.error,
        "goal": run.goal,
        "created_at": _iso(run.created_at),
        "finished_at": _iso(run.finished_at),
        "rounds": run.rounds,
        "tool_calls_total": run.tool_calls,
        "matched_by": matched_by,
        "tool_calls_naming_this_customer": calls,
        "trace_url": f"/api/v1/agent/runs/{run.run_id}/trace.html",
    }


async def debug_customer(db: AsyncSession, customer_id: int) -> Optional[dict]:
    """One customer's full story. ``None`` if there is no such customer."""
    customer = (
        await db.execute(select(Customer).where(Customer.id == customer_id))
    ).scalar_one_or_none()
    if customer is None:
        return None

    now = datetime.utcnow()

    emails = (
        await db.execute(
            select(EmailLog)
            .where(EmailLog.customer_id == customer.id)
            .order_by(EmailLog.created_at.desc())
            .limit(MAX_EMAILS)
        )
    ).scalars().all()

    contacts = (
        await db.execute(
            select(CustomerContact).where(CustomerContact.customer_id == customer.id)
        )
    ).scalars().all()
    primary = next((c for c in contacts if c.is_primary == 1), None)

    notes = (
        await db.execute(
            select(CustomerNote)
            .where(CustomerNote.customer_id == customer.id)
            .order_by(CustomerNote.created_at.desc())
            .limit(20)
        )
    ).scalars().all()

    thread = derive_thread_state(emails, now)

    # The decision, from the same code path the scheduler uses -- with
    # persist=False, so looking does not write the commitment it looked at, and
    # computed for **this customer only**. Asking for the whole book and picking
    # one out of it cost 125 ms per debug request at a thousand customers and
    # grew with the book, which is the wrong shape for a page somebody opens.
    from app.services.agent_service import plan_for_one_customer

    action = await plan_for_one_customer(db, customer, now)

    stage = _status(customer.stage)
    sent = [e for e in emails if _status(e.status) in SENT_STATES]
    replied = [e for e in emails if _status(e.status) == "replied"]
    last = emails[0] if emails else None
    last_contact = customer.last_contacted_at or (last.created_at if last else None)

    churn = assess_churn(
        stage=stage,
        created_days_ago=days_between(now, customer.created_at),
        days_since_contact=days_between(now, last_contact),
        days_in_stage=days_between(now, customer.updated_at or customer.created_at),
        emails_sent=len(sent),
        emails_replied=len(replied),
        last_email_unopened=bool(last is not None and last.opened_at is None),
        last_email_days_ago=days_between(now, last.created_at) if last else None,
        thread=thread,
        thread_policy=DEFAULT_THREAD_POLICY,
    )

    runs = await runs_for_customer(db, customer)

    findings = _findings(
        customer=customer,
        stage=stage,
        emails=emails,
        thread=thread,
        action=action,
        primary=primary,
        runs=runs["runs"],
        churn=churn,
    )

    return {
        "customer": {
            "id": customer.id,
            "company_name": customer.company_name,
            "country": customer.country,
            "industry": customer.industry,
            "website": customer.website,
            "stage": stage,
            "source": _status(customer.source),
            "created_at": _iso(customer.created_at),
            "updated_at": _iso(customer.updated_at),
            "last_contacted_at": _iso(customer.last_contacted_at),
            # The stored commitment, next to the plan's current view of it, so a
            # stale one is visible instead of being silently recomputed.
            "next_follow_up_at": _iso(customer.next_follow_up_at),
        },
        "contact": (
            {"name": primary.name, "email": primary.email, "title": primary.title,
             "is_primary": True}
            if primary is not None
            else None
        ),
        "contact_count": len(contacts),
        "thread": thread.as_dict(),
        "emails": [
            {
                "id": e.id,
                "status": _status(e.status),
                "subject": e.subject,
                "to_email": e.to_email,
                "created_at": _iso(e.created_at),
                "sent_at": _iso(e.sent_at),
                "replied_at": _iso(e.replied_at),
                "opened_at": _iso(e.opened_at),
                "error_message": e.error_message,
                "tracking_id": e.tracking_id,
            }
            for e in emails
        ],
        "emails_shown": len(emails),
        "plan": action,
        # The backlog, so a customer who is not in today's bounded list can be
        # told *that* rather than looking as though nothing is owed. The plan
        # handles twenty a day by default; on a book of a thousand that is a
        # fifty-day queue, and it should not be invisible.
        "backlog": await _backlog(db, now),
        # Same field names as customer_churn_alerts uses, so the two views of the
        # same customer cannot drift into describing the same thing differently.
        "churn": {
            "risk_level": churn.level,
            "risk_reasons": churn.reasons,
            "suggested_actions": churn.actions,
        },
        "runs": runs["runs"],
        "runs_scanned": runs["runs_scanned"],
        "runs_scan_truncated": runs["scan_truncated"],
        "runs_found_by_column": sum(
            1 for r in runs["runs"] if r["matched_by"] == "customer_id column"
        ),
        "legacy_runs_searched": runs["legacy_rows_searched"],
        "notes": [
            {"id": n.id, "content": n.content, "created_at": _iso(n.created_at)}
            for n in notes
        ],
        "findings": findings,
    }


async def _backlog(db: AsyncSession, now: datetime) -> Optional[dict]:
    """How deep the queue is, so "not today" can be distinguished from "never".

    Cheap on purpose: the counts come from two queries rather than from building
    the whole plan, because this sits inside a request somebody is waiting for.
    ``today`` is the plan's own daily cap, read from the policy rather than
    repeated here -- if the two ever disagreed the view would lie.
    """
    from app.models.email_log import EmailStatus
    from app.services.agent_service import plan_policy_stages
    from app.services.follow_up_plan import DEFAULT_FOLLOW_UP_PLAN_POLICY
    from sqlalchemy import func

    waiting = (
        await db.execute(
            select(func.count(Customer.id)).where(
                Customer.stage.in_(list(plan_policy_stages()))
            )
        )
    ).scalar_one()

    drafts = (
        await db.execute(
            select(func.count(EmailLog.id)).where(EmailLog.status == EmailStatus.PENDING)
        )
    ).scalar_one()

    per_day = DEFAULT_FOLLOW_UP_PLAN_POLICY.max_actions_per_day
    return {
        "customers_in_play": waiting,
        "pending_drafts": drafts,
        "max_actions_per_day": per_day,
        # A rough drain estimate, and labelled as one: it assumes every customer
        # ends up owing an action, which is an upper bound on the queue.
        "days_to_clear_if_all_waiting": (
            -(-waiting // per_day) if per_day else None
        ),
    }


def _findings(
    *,
    customer: Customer,
    stage: str,
    emails: list,
    thread,
    action: Optional[dict],
    primary: Optional[CustomerContact],
    runs: list[dict],
    churn,
) -> list[str]:
    """The answer in plain language, most likely cause first.

    Ordered by what actually stops a customer from being handled: no address,
    then a broken last attempt, then our own silence, then the plan's decision.
    """
    findings: list[str] = []

    if primary is None or not (primary.email or "").strip():
        findings.append(
            "No email address on file. Nothing can be sent to this customer, and "
            "the plan can only report them."
        )

    failed = [e for e in emails if _status(e.status) in ("failed", "bounced")]
    if failed:
        newest = failed[0]
        findings.append(
            f"The most recent failed attempt is email {newest.id} "
            f"({_status(newest.status)}): {newest.error_message or 'no reason recorded'}"
        )

    if thread.they_wrote_last:
        owed = thread.reply_owed_days or 0
        if action is not None and action.get("kind") == "review":
            findings.append(
                f"They wrote {owed} day(s) ago and nobody answered. That is past "
                "the point of a routine reply, so the plan asks for a decision "
                "rather than another email."
            )
        else:
            findings.append(f"They wrote {owed} day(s) ago; we owe them an answer.")

    if thread.pending_proposals:
        findings.append(
            f"{thread.pending_proposals} draft(s) are waiting for a human decision "
            f"(oldest {thread.pending_proposal_days or 0} day(s)). Nothing further "
            "is proposed until that decision is made."
        )

    if action is None:
        if stage in ("completed", "lost"):
            findings.append(
                f"The customer is in the closed stage '{stage}', so the plan does "
                "not speak about them at all."
            )
        else:
            findings.append(
                "The plan proposes nothing for this customer right now: nothing is "
                "owed yet, or a commitment already covers them."
            )
    else:
        findings.append(
            f"The plan's next action is '{action.get('kind')}' "
            f"({action.get('priority')}), due {action.get('due_at')}: "
            f"{action.get('reason')}"
        )

    if not runs:
        findings.append(
            "No agent run has ever mentioned this customer. If an analysis was "
            "expected, it did not reach them."
        )

    if churn.level in ("critical", "high"):
        findings.append(f"Churn assessment: {churn.level} - {'; '.join(churn.reasons[:2])}")

    return findings
