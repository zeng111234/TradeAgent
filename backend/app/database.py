"""Database connection and session management."""
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

#: Pool shape, stated rather than inherited. Measured (tests/test_pool_capacity.py):
#:
#:     pool 5 + 10 overflow = 15 sessions
#:     100 concurrent light requests  -> 0.94s wall, p50 500 ms, 0 errors
#:     100 concurrent plan requests   -> 5.97s wall, p95 5.8 s, 0 errors
#:     /health during both            -> p50 0 ms, max 16 ms
#:
#: So the event loop is not the constraint; the pool is, and it does not fail at a
#: hundred -- it queues. Fifteen is kept because more connections do not buy
#: throughput here: every aiosqlite connection is another thread serialising rows
#: through one GIL, and SQLite admits one writer regardless. Measured elsewhere in
#: this session: running the same work concurrently took *twice* as long as
#: running it back to back. Raising the pool would trade queueing for contention.
DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 10

#: How long a request waits for a free connection before giving up. Thirty
#: seconds was the inherited default, and it is the wrong number: a caller is
#: better served by an immediate, honest "busy" than by a request that hangs for
#: half a minute and then fails as an unhandled 500. With this, exhaustion is a
#: 503 with a Retry-After header (see app/main.py).
DEFAULT_POOL_TIMEOUT_SECONDS = 5.0

# A long analysis writes its progress while requests are being served, and
# SQLite admits one writer at a time. The default is to give up after five
# seconds with "database is locked"; waiting longer is the right trade for a
# single-process deployment, because the alternative is silently losing a write.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=DEFAULT_POOL_SIZE,
    max_overflow=DEFAULT_MAX_OVERFLOW,
    pool_timeout=DEFAULT_POOL_TIMEOUT_SECONDS,
    connect_args={"timeout": 30} if settings.DATABASE_URL.startswith("sqlite") else {},
)


if settings.DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        """Set the pragmas SQLite needs to behave under concurrency.

        ``journal_mode=WAL`` is the important one. The default rollback journal
        makes a writer block every reader for the duration of its transaction, so
        several requests that only read end up waiting behind each other. WAL
        lets readers proceed against the last committed state while one writer
        works -- which is what makes the async server actually concurrent rather
        than merely await-shaped. Measured on this codebase: four concurrent
        whole-book plan runs took 2.05 s against 1.00 s sequentially without it;
        see tests/test_concurrency_isolation.py for the regression guard.

        ``synchronous=NORMAL`` is the usual companion: with WAL it fsyncs at
        checkpoints rather than every commit, which is the documented trade for
        this journal mode.

        **Not** enabled here: ``foreign_keys=ON``. It is off by default in SQLite,
        which is why ``POST /agent/run`` validates a customer id itself rather
        than trusting the database. Turning it on can fail writes for rows that
        already violate a constraint, so it is a migration-shaped decision, not a
        pragma to slip in.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

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