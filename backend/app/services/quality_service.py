"""Operational metrics: is the system getting worse?

The distinction this module exists to make
------------------------------------------
``/analytics/dashboard`` measures the **business** (customers, open rate, replies).
``/agent/runs`` and ``/scheduler/history`` are **single slices**: what happened in
one run, one job. Neither answers "this week compared with last week", and until
now nothing did. That is why a regression could only be noticed by a person
reading a trace or a red test run.

So this module aggregates the rows already being written, over a window:

* ``agent_runs``  -- status mix, success rate, latency, token spend, stop reasons
* ``job_runs``    -- did the scheduled work happen, including the skipped runs
* ``email_logs``  -- was the human accepting the drafts the system proposed

What it deliberately does **not** do
------------------------------------
* **It never reads ``trace``.** A run's trace is the largest column in the table
  and irrelevant to a count. Selecting it would turn a dashboard refresh into a
  multi-megabyte read.
* **It does not invent a quality score.** There is no answer-quality rubric in
  this repository, and averaging the numbers that do exist into one "health
  score" would be judgement dressed as arithmetic. The caller sees the parts.
* **It does not make the suggestion→reply loop look cleaner than it is.** The
  loop is now measurable (``email_logs.suggestion_kind`` is written at send
  time), but a reply rate per *email* is not a reply rate per *customer*: three
  chases to the same person are not three independent observations. The response
  carries that caveat next to the numbers instead of leaving it to the reader.

Percentiles are computed from the elapsed values themselves rather than with a
SQL percentile function: SQLite has none, and a nearest-rank approximation that
differs between SQLite and PostgreSQL is a metric that cannot be compared across
deployments.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.email_log import EmailLog, EmailStatus
from app.models.job_run import ABANDONED, FAILED, RUNNING, SKIPPED, SUCCEEDED, JobRun
from app.suggestion import UNATTRIBUTED

#: Statuses that mean "the run finished and did not succeed".
#: ``cancelled`` is intentionally absent: an operator stopping a healthy run is
#: not a quality failure. It falls into ``runs_other`` and leaves the success
#: rate's numerator and denominator unchanged.
RUN_FAILED = ("error", "stalled", "truncated")

#: A mail in one of these states reached the recipient, or at least left this
#: process with a server's acknowledgement. Only these can be replied to, so
#: only these belong in a reply-rate denominator.
SENT_STATES = frozenset({
    EmailStatus.SENT, EmailStatus.DELIVERED,
    EmailStatus.OPENED, EmailStatus.REPLIED,
})

#: Attempts are small. Anything past this is folded into ``5+`` so a data-entry
#: accident cannot turn the by-attempt table into a long tail of one-row buckets.
MAX_ATTEMPT_BUCKET = 5

#: How many stop reasons / prompt versions to return. The tail is counted in
#: ``other`` rather than dropped, so the list cannot silently hide a new reason.
TOP_N = 10


def _percentile(values: list[int], fraction: float) -> Optional[float]:
    """Linear-interpolated percentile of a non-empty list.

    Returns ``None`` for an empty list rather than ``0``: "no runs" and "all runs
    took zero milliseconds" are different facts, and this repository has been
    bitten before by ``0`` standing in for "unknown" (``app.llm_usage``).
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 1)


def _top(counter: dict[str, int], *, limit: Optional[int] = None) -> list[dict]:
    """Top entries plus an explicit ``other`` count.

    A free-form field (``stop_reason`` is a sentence) cannot be grouped exactly.
    Returning only the top N would make the remainder invisible; the tail is
    reported as a count so a new reason shows up as movement in ``other``.

    ``limit`` defaults to ``TOP_N`` at call time rather than at definition time,
    so the constant stays patchable -- a default argument would bind the old
    value and quietly ignore the change.
    """
    limit = TOP_N if limit is None else limit
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    head = ordered[:limit]
    tail = ordered[limit:]
    result = [{"value": value, "count": count} for value, count in head]
    if tail:
        result.append({"value": "(other)", "count": sum(count for _, count in tail),
                       "distinct": len(tail)})
    return result


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100, 1)


def _attempt_bucket(attempt: Optional[int]) -> str:
    """The label an attempt count is grouped under.

    ``unknown`` is a real bucket, not a placeholder: a caller can know which
    suggestion produced a mail without knowing how many tries preceded it, and
    collapsing that into ``0`` would report first contacts that were not.
    """
    if attempt is None:
        return "unknown"
    if attempt >= MAX_ATTEMPT_BUCKET:
        return f"{MAX_ATTEMPT_BUCKET}+"
    return str(attempt)


def _bucket_order(label: str) -> tuple[int, int | str]:
    """Sort attempts numerically, with ``unknown`` last."""
    if label == "unknown":
        return (1, 0)
    return (0, int(label.rstrip("+")))


def _replied(row) -> bool:
    """Did this mail get an answer?

    Two fields can say so and both mean the same thing: ``replied_at`` is what
    the reply endpoint writes, ``status=replied`` is what it derives. Reading
    either keeps one malformed row from silently removing a reply from the
    numerator while it stays in the denominator.
    """
    return row.replied_at is not None or row.status == EmailStatus.REPLIED


def _closed_loop(rows: list) -> dict:
    """Reply rate by what was suggested, and by how many tries it took.

    The denominator is *delivered attempts*: a mail that left this process and
    was not a simulation, because a simulated send cannot receive a reply and
    counting it would make every rate depend on whether SMTP is configured.
    Bounced and failed mail is excluded for the same reason and counted
    separately, so the exclusion is visible instead of being an unexplained gap
    between ``sent`` here and ``sent`` in ``approvals``.
    """
    delivered = [r for r in rows if r.status in SENT_STATES and not r.simulated]
    excluded = {
        "simulated": sum(1 for r in rows if r.simulated and r.status in SENT_STATES),
        "bounced": sum(1 for r in rows if r.status == EmailStatus.BOUNCED),
        "failed": sum(1 for r in rows if r.status == EmailStatus.FAILED),
    }

    by_kind: dict[str, dict] = {}
    by_attempt: dict[str, dict] = {}
    attributed = 0
    replied_total = 0

    for row in delivered:
        replied = _replied(row)
        replied_total += int(replied)
        kind = row.suggestion_kind or UNATTRIBUTED
        attributed += int(row.suggestion_kind is not None)

        for table, label in ((by_kind, kind),
                             (by_attempt, _attempt_bucket(row.suggestion_attempt))):
            entry = table.setdefault(label, {"label": label, "sent": 0, "replied": 0})
            entry["sent"] += 1
            entry["replied"] += int(replied)

    def finish(table: dict, key: str) -> list[dict]:
        items = [dict(entry, reply_rate=_rate(entry["replied"], entry["sent"]))
                 for entry in table.values()]
        if key == "attempt":
            items.sort(key=lambda item: _bucket_order(item["label"]))
        else:
            items.sort(key=lambda item: (-item["sent"], item["label"]))
        return [{key: item.pop("label"), **item} for item in items]

    return {
        "available": True,
        "sent": len(delivered),
        "replied": replied_total,
        "reply_rate": _rate(replied_total, len(delivered)),
        "attributed": attributed,
        "unattributed": len(delivered) - attributed,
        "coverage": _rate(attributed, len(delivered)),
        "excluded": excluded,
        "by_kind": finish(by_kind, "kind"),
        "by_attempt": finish(by_attempt, "attempt"),
        "note": (
            "reply_rate is per delivered email, not per customer: several "
            "attempts to the same person are not independent observations, so "
            "compare buckets rather than reading a bucket as a probability."
        ),
        "caveats": [
            "provenance is recorded when the mail is sent and never rewritten; "
            "an email composed by hand, or sent before migration 0010, is "
            "counted under 'unattributed'",
            "a reply is recorded against the email a human says it answers; a "
            "reply to the first chase that is filed against the second one is "
            "indistinguishable here",
            "simulated sends (no SMTP configured) are excluded: they cannot "
            "receive a reply, and including them would tie the rate to whether "
            "credentials exist",
        ],
    }


def _window(days: int, now: datetime) -> tuple[datetime, list[str]]:
    """Start of the earliest day, and every UTC date label in the window."""
    today = now.date()
    start_day = today - timedelta(days=days - 1)
    since = datetime.combine(start_day, datetime.min.time())
    labels = [(start_day + timedelta(days=offset)).isoformat()
              for offset in range(days)]
    return since, labels


def _run_day(row) -> dict:
    return {
        "date": row.created_at.date().isoformat(),
        "status": row.status,
        "elapsed": row.elapsed or 0,
        "rounds": row.rounds or 0,
        "tool_calls": row.tool_calls or 0,
        "prompt_tokens": row.prompt_tokens or 0,
        "completion_tokens": row.completion_tokens or 0,
        "cached_prompt_tokens": row.cached_prompt_tokens or 0,
        "stop_code": row.stop_code,
        "stop_reason": row.stop_reason,
        "prompt_version": row.prompt_version,
        "created_at": row.created_at,
    }


async def get_quality_report(db: AsyncSession, *, days: int = 14,
                             now: Optional[datetime] = None) -> dict:
    """The window's operational metrics. Read-only, and never touches traces."""
    now = now or datetime.utcnow()
    since, labels = _window(days, now)

    # Explicit column list: ``trace`` is the reason a naive ``select(AgentRun)``
    # would be slow, and no metric here needs it.
    run_rows = (await db.execute(
        select(
            AgentRun.created_at,
            AgentRun.status,
            AgentRun.elapsed,
            AgentRun.rounds,
            AgentRun.tool_calls,
            AgentRun.prompt_tokens,
            AgentRun.completion_tokens,
            AgentRun.cached_prompt_tokens,
            AgentRun.stop_code,
            AgentRun.stop_reason,
            AgentRun.prompt_version,
        ).where(AgentRun.created_at >= since, AgentRun.created_at <= now)
    )).all()

    job_rows = (await db.execute(
        select(
            JobRun.job_id,
            JobRun.started_at,
            JobRun.status,
            JobRun.elapsed_ms,
        ).where(JobRun.started_at >= since, JobRun.started_at <= now)
    )).all()

    email_rows = (await db.execute(
        select(
            EmailLog.created_at,
            EmailLog.status,
            EmailLog.simulated,
            EmailLog.replied_at,
            EmailLog.suggestion_kind,
            EmailLog.suggestion_attempt,
        ).where(EmailLog.created_at >= since, EmailLog.created_at <= now)
    )).all()

    runs = [_run_day(row) for row in run_rows]

    # ---- runs ------------------------------------------------------------
    by_status: dict[str, int] = {}
    stop_codes: dict[str, int] = {}
    stop_reasons: dict[str, int] = {}
    prompt_versions: dict[str, int] = {}
    elapsed_values: list[int] = []
    totals = {"rounds": 0, "tool_calls": 0, "prompt_tokens": 0,
              "completion_tokens": 0, "cached_prompt_tokens": 0}

    for run in runs:
        by_status[run["status"]] = by_status.get(run["status"], 0) + 1
        elapsed_values.append(run["elapsed"])
        for key in totals:
            totals[key] += run[key]
        reason = (run["stop_reason"] or "(none)")[:160]
        stop_reasons[reason] = stop_reasons.get(reason, 0) + 1
        code = run["stop_code"] or "(none)"
        stop_codes[code] = stop_codes.get(code, 0) + 1
        version = run["prompt_version"] or "(none)"
        prompt_versions[version] = prompt_versions.get(version, 0) + 1

    finished = len(runs) - by_status.get("running", 0)
    succeeded = by_status.get("succeeded", 0)
    runs_summary = {
        "total": len(runs),
        "by_status": by_status,
        "success_rate": _rate(succeeded, finished),
        "elapsed_ms": {
            "p50": _percentile(elapsed_values, 0.50),
            "p95": _percentile(elapsed_values, 0.95),
            "max": float(max(elapsed_values)) if elapsed_values else None,
        },
        "totals": totals,
        "cached_prompt_ratio": (
            round(totals["cached_prompt_tokens"] / totals["prompt_tokens"] * 100, 1)
            if totals["prompt_tokens"] else None
        ),
        "by_stop_code": _top(stop_codes),
        "by_stop_reason": _top(stop_reasons),
        "by_prompt_version": _top(prompt_versions),
    }

    # ---- jobs ------------------------------------------------------------
    job_status: dict[str, int] = {}
    job_by_id: dict[str, dict] = {}
    for row in job_rows:
        job_status[row.status] = job_status.get(row.status, 0) + 1
        entry = job_by_id.setdefault(
            row.job_id,
            {"job_id": row.job_id, "total": 0, SUCCEEDED: 0, FAILED: 0,
             SKIPPED: 0, ABANDONED: 0, RUNNING: 0},
        )
        entry["total"] += 1
        entry[row.status] = entry.get(row.status, 0) + 1

    settled_jobs = (job_status.get(SUCCEEDED, 0) + job_status.get(FAILED, 0)
                    + job_status.get(ABANDONED, 0))
    jobs_summary = {
        "total": len(job_rows),
        "by_status": job_status,
        "success_rate": _rate(job_status.get(SUCCEEDED, 0), settled_jobs),
        "by_job": sorted(job_by_id.values(), key=lambda item: item["job_id"]),
        "note": (
            "success_rate excludes skipped: a skipped run is a recorded "
            "non-run, not a failed one. It is counted in by_status so it cannot "
            "disappear."
        ),
    }

    # ---- human-in-the-loop ----------------------------------------------
    proposals = len(email_rows)
    sent = sum(1 for row in email_rows if row.status in SENT_STATES)
    rejected = sum(1 for row in email_rows if row.status == EmailStatus.REJECTED)
    pending = sum(1 for row in email_rows
                  if row.status in (EmailStatus.PENDING, EmailStatus.SENDING))
    approvals = {
        "proposals": proposals,
        "sent": sent,
        "rejected": rejected,
        "pending": pending,
        "acceptance_rate": _rate(sent, sent + rejected),
        "note": (
            "acceptance_rate is decided proposals only (sent / (sent + rejected)); "
            "pending drafts are excluded because nobody has decided yet."
        ),
    }

    # ---- daily -----------------------------------------------------------
    daily = {label: {"date": label, "runs_total": 0, "runs_succeeded": 0,
                     "runs_failed": 0, "runs_truncated": 0, "runs_other": 0,
                     "jobs_total": 0, "jobs_succeeded": 0, "jobs_failed": 0,
                     "jobs_skipped": 0}
             for label in labels}

    for run in runs:
        bucket = daily.get(run["created_at"].date().isoformat())
        if bucket is None:
            continue
        bucket["runs_total"] += 1
        if run["status"] == "succeeded":
            bucket["runs_succeeded"] += 1
        elif run["status"] == "truncated":
            bucket["runs_truncated"] += 1
        elif run["status"] in RUN_FAILED:
            bucket["runs_failed"] += 1
        else:
            bucket["runs_other"] += 1

    for row in job_rows:
        bucket = daily.get(row.started_at.date().isoformat())
        if bucket is None:
            continue
        bucket["jobs_total"] += 1
        if row.status == SUCCEEDED:
            bucket["jobs_succeeded"] += 1
        elif row.status in (FAILED, ABANDONED):
            bucket["jobs_failed"] += 1
        elif row.status == SKIPPED:
            bucket["jobs_skipped"] += 1

    return {
        "window": {"days": days, "since": labels[0], "until": labels[-1]},
        "generated_at": now.isoformat(timespec="seconds"),
        "runs": runs_summary,
        "jobs": jobs_summary,
        "approvals": approvals,
        "daily": [daily[label] for label in labels],
        "closed_loop": _closed_loop(email_rows),
    }
