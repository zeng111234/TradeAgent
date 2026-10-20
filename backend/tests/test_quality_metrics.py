"""The quality report: counts what is in the rows, and says what it cannot know.

The assertions are about the two failure modes of a metrics endpoint:

* **a missing day looks like a good day** -- so every day in the window is
  present, including zeroes;
* **a free-form field is silently truncated** -- so ``stop_reason`` returns the
  tail as an explicit ``(other)`` count.

And one guard that matters more than either: the query must not read ``trace``.
A dashboard that pulls the largest column in the table is the reason nobody
refreshes it.
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import delete, event

from app.models.agent_run import AgentRun as AgentRunRow
from app.models.email_log import EmailLog, EmailStatus
from app.models.job_run import JobRun
from app.services.quality_service import get_quality_report, _percentile

#: A window in the past, on purpose. The test database is shared across the
#: session and other files write runs "now"; a 2020 window plus the service's
#: upper bound keeps those rows out of the assertions.
NOW = datetime(2020, 1, 15, 12, 0, 0)


def _run(day: int, *, status: str, elapsed: int, reason: str = "done",
         version: str | None = "abc123", **extra):
    return AgentRunRow(
        run_id=f"quality-{uuid4().hex}",
        goal="test",
        status=status,
        elapsed=elapsed,
        stop_reason=reason,
        prompt_version=version,
        created_at=datetime(2020, 1, day, 9, 0, 0),
        **extra,
    )


async def _seed(db):
    # The test database is shared for the session, so a previous test's rows are
    # still there. Every row this file writes carries a ``quality-`` marker;
    # removing them first makes each case independent of collection order.
    await db.execute(delete(AgentRunRow).where(AgentRunRow.run_id.like("quality-%")))
    await db.execute(delete(JobRun).where(JobRun.job_id.like("quality-%")))
    await db.execute(delete(EmailLog).where(EmailLog.to_email.like("quality-%")))
    await db.commit()

    db.add_all([
        _run(13, status="succeeded", elapsed=100, rounds=2, tool_calls=3,
             prompt_tokens=1000, completion_tokens=100, cached_prompt_tokens=400),
        _run(13, status="error", elapsed=300, reason="boom", version="abc123"),
        _run(14, status="succeeded", elapsed=200, rounds=1, tool_calls=1,
             prompt_tokens=500, completion_tokens=50, cached_prompt_tokens=0),
        _run(15, status="truncated", elapsed=400, reason="cut off",
             version=None, rounds=5),
    ])
    db.add_all([
        JobRun(job_id="quality-daily", status="succeeded",
               started_at=datetime(2020, 1, 13, 22, 0)),
        JobRun(job_id="quality-daily", status="skipped",
               started_at=datetime(2020, 1, 14, 22, 0)),
        JobRun(job_id="quality-churn", status="failed",
               started_at=datetime(2020, 1, 15, 4, 0)),
    ])
    db.add_all([
        EmailLog(to_email="quality-a@example.test", subject="s", body="b",
                 status=EmailStatus.SENT,
                 created_at=datetime(2020, 1, 13, 10, 0)),
        EmailLog(to_email="quality-b@example.test", subject="s", body="b",
                 status=EmailStatus.REJECTED,
                 created_at=datetime(2020, 1, 14, 10, 0)),
        EmailLog(to_email="quality-c@example.test", subject="s", body="b",
                 status=EmailStatus.PENDING,
                 created_at=datetime(2020, 1, 15, 10, 0)),
        EmailLog(to_email="quality-d@example.test", subject="s", body="b",
                 status=EmailStatus.REPLIED,
                 created_at=datetime(2020, 1, 15, 11, 0)),
    ])
    await db.commit()


async def _report(db, days=3):
    await _seed(db)
    return await get_quality_report(db, days=days, now=NOW)


async def test_the_status_mix_and_success_rate_exclude_the_running_race(db):
    report = await _report(db)

    assert report["runs"]["total"] == 4
    assert report["runs"]["by_status"] == {
        "succeeded": 2, "error": 1, "truncated": 1,
    }
    # 2 of 4 finished runs. A run still in flight is not a failure.
    assert report["runs"]["success_rate"] == 50.0


async def test_a_rate_with_no_finished_runs_is_null_not_zero(db):
    """``0`` would claim every run failed; there were no runs to fail."""
    report = await get_quality_report(
        db, days=1, now=datetime(2020, 2, 1, 12, 0, 0)
    )
    assert report["runs"]["total"] == 0
    assert report["runs"]["success_rate"] is None
    assert report["runs"]["elapsed_ms"]["p50"] is None


async def test_latency_percentiles_are_computed_from_the_run_rows(db):
    report = await _report(db)
    elapsed = report["runs"]["elapsed_ms"]

    # [100, 200, 300, 400] -> p50 at position 1.5, p95 at 2.85.
    assert elapsed["p50"] == 250.0
    assert elapsed["p95"] == 385.0
    assert elapsed["max"] == 400.0


async def test_token_totals_and_the_cache_ratio_are_reported(db):
    report = await _report(db)
    totals = report["runs"]["totals"]

    assert totals["prompt_tokens"] == 1500
    assert totals["completion_tokens"] == 150
    assert totals["cached_prompt_tokens"] == 400
    assert report["runs"]["cached_prompt_ratio"] == 26.7


async def test_stop_reasons_keep_the_tail_as_an_explicit_other(db, monkeypatch):
    from app.services import quality_service

    monkeypatch.setattr(quality_service, "TOP_N", 1)
    report = await _report(db)

    reasons = report["runs"]["by_stop_reason"]
    assert reasons[0]["count"] == 2  # "done" x2
    assert reasons[-1]["value"] == "(other)"
    assert reasons[-1]["count"] == 2
    assert reasons[-1]["distinct"] == 2


async def test_a_day_with_no_runs_is_present_and_zero(db):
    report = await _report(db, days=6)

    labels = [day["date"] for day in report["daily"]]
    assert labels[0] == "2020-01-10"
    assert labels[-1] == "2020-01-15"
    empty = report["daily"][0]
    assert empty["runs_total"] == 0 and empty["jobs_total"] == 0


async def test_the_daily_series_splits_success_failure_and_truncation(db):
    report = await _report(db)
    by_date = {day["date"]: day for day in report["daily"]}

    assert by_date["2020-01-13"]["runs_succeeded"] == 1
    assert by_date["2020-01-13"]["runs_failed"] == 1
    assert by_date["2020-01-15"]["runs_truncated"] == 1
    assert by_date["2020-01-14"]["jobs_skipped"] == 1


async def test_job_success_rate_counts_skipped_as_separate(db):
    report = await _report(db)

    assert report["jobs"]["total"] == 3
    assert report["jobs"]["by_status"] == {
        "succeeded": 1, "skipped": 1, "failed": 1,
    }
    # skipped is recorded, but it is not a failed run and not a success.
    assert report["jobs"]["success_rate"] == 50.0


async def test_draft_acceptance_is_decided_proposals_only(db):
    report = await _report(db)
    approvals = report["approvals"]

    assert approvals["proposals"] == 4
    assert approvals["sent"] == 2       # sent + replied
    assert approvals["rejected"] == 1
    assert approvals["pending"] == 1
    assert approvals["acceptance_rate"] == 66.7


async def test_the_suggestion_to_reply_loop_is_declared_unavailable(db):
    """The honest gap: kind/attempt are not persisted, so there is no grouping."""
    report = await _report(db)

    assert report["closed_loop"]["available"] is False
    assert "never stored" in report["closed_loop"]["reason"]


async def test_the_query_never_reads_the_trace_column(db):
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    await _seed(db)
    event.listen(db.bind.sync_engine, "before_cursor_execute", record)
    try:
        await get_quality_report(db, days=3, now=NOW)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", record)

    offending = [statement for statement in statements if "agent_runs.trace" in statement]
    assert not offending, (
        "the quality report read the trace column; a dashboard refresh would "
        "pull every run's full transcript:\n  " + "\n  ".join(offending)
    )


async def test_the_endpoint_returns_the_report_and_validates_the_window(client):
    response = client.get("/api/v1/analytics/quality?days=3")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["window"]["days"] == 3
    assert len(body["daily"]) == 3
    assert "success_rate" in body["runs"]

    assert client.get("/api/v1/analytics/quality?days=0").status_code == 422
    assert client.get("/api/v1/analytics/quality?days=91").status_code == 422


def test_percentile_returns_none_for_no_values_and_handles_one():
    assert _percentile([], 0.5) is None
    assert _percentile([7], 0.95) == 7.0
    assert _percentile([0, 100], 0.5) == 50.0
