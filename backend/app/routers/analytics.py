"""Analytics and dashboard API routes."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.analytics import (
    DashboardStats, PipelineFunnel, SourceStats,
    CountryStats, MonthlyTrend, QualityReport,
)
from app.services.analytics_service import (
    get_dashboard_stats, get_pipeline_funnel,
    get_source_stats, get_country_stats, get_monthly_trends,
)
from app.services.quality_service import get_quality_report

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/dashboard", response_model=DashboardStats)
async def dashboard(db: AsyncSession = Depends(get_db)):
    """Get main dashboard statistics."""
    return await get_dashboard_stats(db)


@router.get("/pipeline", response_model=PipelineFunnel)
async def pipeline(db: AsyncSession = Depends(get_db)):
    """Get sales pipeline funnel data."""
    return await get_pipeline_funnel(db)


@router.get("/sources", response_model=list[SourceStats])
async def sources(db: AsyncSession = Depends(get_db)):
    """Get customer acquisition source statistics."""
    return await get_source_stats(db)


@router.get("/countries", response_model=list[CountryStats])
async def countries(db: AsyncSession = Depends(get_db)):
    """Get customer distribution by country."""
    return await get_country_stats(db)


@router.get("/trends", response_model=list[MonthlyTrend])
async def trends(months: int = 6, db: AsyncSession = Depends(get_db)):
    """Get monthly trend data."""
    return await get_monthly_trends(db, months=months)


@router.get("/quality", response_model=QualityReport)
async def quality(
    days: int = Query(14, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    """Operational trend for the last ``days`` UTC days.

    Answers "is the system getting worse": run status mix and success rate,
    latency percentiles, token spend, stop reasons, scheduled-job outcomes, and
    how often a human accepted a proposed draft. It is **not** an answer-quality
    score -- this repository has no rubric for one, and inventing a composite
    number would hide exactly the parts that need looking at.

    Days with no activity are returned as zeroes, not omitted: a gap is a fact.
    ``trace`` is never read.
    """
    return await get_quality_report(db, days=days)