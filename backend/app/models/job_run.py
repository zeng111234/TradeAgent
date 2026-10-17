"""JobRun — the record of what the scheduler actually did.

APScheduler's ``max_instances=1`` prevents a *duplicate* execution of one job
inside one process, by refusing the second fire. Two things it does not do, and
both of them are about the thing a human cares about -- whether the work
happened:

* **It does not tell anyone.** The refused fire writes a log line and nothing
  else. A skipped morning report and a morning report that was never scheduled
  look identical from every place a person can look.
* **It does not span processes.** Two workers mean two schedulers, each with its
  own ``max_instances``, and the same job runs twice.

So the state lives here, in one table, and the database enforces the interesting
part: a partial unique index on ``(job_id) WHERE status = 'running'`` means at
most one row per job can be in flight **in that database**, whichever process
wrote it. The second claim raises ``IntegrityError`` rather than running.

A crash leaves a ``running`` row, which would block the job forever, so stale
rows are reaped by age -- at claim time and at startup. That is the same trade as
``mark_stale_runs`` for agent runs: an abandoned claim is resolved to a recorded
failure, never to silence.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, String, Text, text

from app.database import Base

#: A run still marked ``running`` after this long is not running any more.
STALE_AFTER_SECONDS = 3600

#: The states a run can be recorded in. ``skipped`` is the one that matters:
#: it is how "we did not run, and here is why" gets written down.
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"
ABANDONED = "abandoned"


class JobRun(Base):
    """One attempt at one scheduled job."""

    __tablename__ = "job_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(80), nullable=False, index=True)
    #: What started it: ``schedule`` or ``manual``. Both go through the same
    #: claim, which is the point -- a human clicking "run now" while the cron is
    #: working is exactly the duplicate that ``max_instances`` cannot see.
    trigger = Column(String(20), nullable=False, default="schedule")
    status = Column(String(20), nullable=False, default=RUNNING)
    detail = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    elapsed_ms = Column(Integer, nullable=True)

    __table_args__ = (
        # The guarantee, and it is the database's rather than the application's:
        # at most one in-flight run per job, whoever is trying.
        Index(
            "uq_job_runs_one_running",
            "job_id",
            unique=True,
            sqlite_where=text("status = 'running'"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<JobRun(job_id='{self.job_id}', status='{self.status}')>"
