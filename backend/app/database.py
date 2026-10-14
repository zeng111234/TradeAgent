"""Database connection and session management."""
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=settings.DEBUG)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


async def get_db() -> AsyncSession:
    """Dependency for getting database session."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """Create every table.

    Imports the models package first. Without it, ``Base.metadata`` contains
    only whatever the importing module happened to pull in — so calling this
    from a fresh process (a CLI, a cron job, a test) would silently create a
    database with no tables, and the failure would only surface later as
    "no such table". Registering the models explicitly makes the function mean
    what its name says.
    """
    import app.models  # noqa: F401  (registers every table on Base.metadata)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)