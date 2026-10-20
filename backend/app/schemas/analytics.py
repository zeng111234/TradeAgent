"""Analytics and dashboard schemas."""
from pydantic import BaseModel


class DashboardStats(BaseModel):
    """Main dashboard statistics."""
    total_customers: int = 0
    new_customers_this_month: int = 0
    total_emails_sent: int = 0
    emails_sent_this_month: int = 0
    open_rate: float = 0.0
    reply_rate: float = 0.0
    pending_tasks: int = 0
    overdue_tasks: int = 0


class StageCount(BaseModel):
    """Customer count per pipeline stage."""
    stage: str
    count: int
    percentage: float = 0.0


class PipelineFunnel(BaseModel):
    """Sales pipeline funnel data."""
    stages: list[StageCount]
    total: int = 0


class SourceStats(BaseModel):
    """Customer acquisition source statistics."""
    source: str
    count: int
    percentage: float = 0.0


class CountryStats(BaseModel):
    """Customer distribution by country."""
    country: str
    count: int


class MonthlyTrend(BaseModel):
    """Monthly trend data point."""
    month: str  # e.g. "2026-01"
    customers_added: int = 0
    emails_sent: int = 0
    emails_opened: int = 0
    emails_replied: int = 0


class QualityWindow(BaseModel):
    """The window the report covers, inclusive of both ends."""
    days: int
    since: str
    until: str


class QualityRunStats(BaseModel):
    """What the agent runs did."""
    total: int = 0
    by_status: dict[str, int] = {}
    #: ``null`` when no run finished -- not 0%, which would mean every run failed.
    success_rate: float | None = None
    elapsed_ms: dict[str, float | None] = {}
    totals: dict[str, int] = {}
    cached_prompt_ratio: float | None = None
    by_stop_code: list[dict] = []
    by_stop_reason: list[dict] = []
    by_prompt_version: list[dict] = []


class QualityJobStats(BaseModel):
    """What the scheduler did, including the runs it skipped."""
    total: int = 0
    by_status: dict[str, int] = {}
    success_rate: float | None = None
    by_job: list[dict] = []
    note: str = ""


class QualityApprovals(BaseModel):
    """Whether a human accepted the drafts the system proposed."""
    proposals: int = 0
    sent: int = 0
    rejected: int = 0
    pending: int = 0
    acceptance_rate: float | None = None
    note: str = ""


class QualityDay(BaseModel):
    """One UTC day. Zero days are present, not omitted."""
    date: str
    runs_total: int = 0
    runs_succeeded: int = 0
    runs_failed: int = 0
    runs_truncated: int = 0
    runs_other: int = 0
    jobs_total: int = 0
    jobs_succeeded: int = 0
    jobs_failed: int = 0
    jobs_skipped: int = 0


class QualitySuggestionBucket(BaseModel):
    """One row of the suggestion→reply table."""
    sent: int = 0
    replied: int = 0
    #: ``null`` when the bucket has no delivered mail -- not 0%, which would
    #: read as "every one of them was ignored".
    reply_rate: float | None = None


class QualitySuggestionKindBucket(QualitySuggestionBucket):
    #: A planned action, or ``unattributed`` for mail with no provenance.
    kind: str


class QualitySuggestionAttemptBucket(QualitySuggestionBucket):
    #: ``"0"`` is a first contact, ``"unknown"`` means the kind was recorded
    #: without an attempt count. A string because of that word.
    attempt: str


class QualityClosedLoop(BaseModel):
    """Reply rate by what was suggested -- the loop, measured.

    Says what it excludes and why. A rate whose denominator nobody can
    reconstruct is a number that will be argued about instead of used.
    """
    available: bool = True
    sent: int = 0
    replied: int = 0
    reply_rate: float | None = None
    attributed: int = 0
    unattributed: int = 0
    #: Share of delivered mail that carried a suggestion kind.
    coverage: float | None = None
    excluded: dict[str, int] = {}
    by_kind: list[QualitySuggestionKindBucket] = []
    by_attempt: list[QualitySuggestionAttemptBucket] = []
    note: str = ""
    caveats: list[str] = []


class QualityReport(BaseModel):
    """Operational metrics for a window -- not a score, and not answer quality."""
    window: QualityWindow
    generated_at: str
    runs: QualityRunStats
    jobs: QualityJobStats
    approvals: QualityApprovals
    daily: list[QualityDay]
    closed_loop: QualityClosedLoop