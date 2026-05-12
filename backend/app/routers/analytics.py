"""Analytics and dashboard API routes."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.analytics import (
    DashboardStats, PipelineFunnel, SourceStats,
    CountryStats, MonthlyTrend,
)
from app.services.analytics_service import (
    get_dashboard_stats, get_pipeline_funnel,
    get_source_stats, get_country_stats, get_monthly_trends,
)

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