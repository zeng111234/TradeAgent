"""Database connection and session management."""
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# A long analysis now writes its progress while requests are being served, and
# SQLite admits one writer at a time. The default is to give up after five
# seconds with "database is locked"; waiting longer is the right trade for a
# single-process deployment, because the alternative is silently losing a write.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    connect_args={"timeout": 30} if settings.DATABASE_URL.startswith("sqlite") else {},
)

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
    """Create every table, then bring an existing database up to date.

    Imports the models package first. Without it, ``Base.metadata`` contains
    only whatever the importing module happened to pull in — so calling this
    from a fresh process (a CLI, a cron job, a test) would silently create a
    database with no tables, and the failure would only surface later as
    "no such table". Registering the models explicitly makes the function mean
    what its name says.

    ``create_all`` handles a database that does not exist. It does nothing at all
    to one that does — no new columns, ever — which is why ``apply_migrations``
    runs next: see ``app/migrations.py`` for what that constraint cost.
    """
    import logging

    import app.models  # noqa: F401  (registers every table on Base.metadata)

    from app.migrations import apply_migrations

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        report = await apply_migrations(conn)

    logging.getLogger(__name__).info(
        "schema: %d migration(s) applied, %d already present, %d pending",
        len(report["applied"]), len(report["already_present"]), len(report["pending"]),
    )
    if report["pending"]:
        # Loud, because it means a migration ran and did not take effect, or the
        # guard and the version table disagree about the same database.
        logging.getLogger(__name__).warning(
            "schema migrations still pending after this run: %s", report["pending"]
        )