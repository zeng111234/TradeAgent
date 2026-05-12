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