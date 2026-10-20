"""Schema changes, applied to databases that already exist.

**The decision this file changes.** Until now the schema was
``Base.metadata.create_all`` and nothing else: tables are created if missing, and
a database that already exists is never touched again. That is fine for a green
field and wrong for everything afterwards, and it shaped a surprising amount of
this codebase. Every one of these is a workaround for it:

* ``EmailStatus.SENDING`` -- adding a *member* was safe only because SQLite stores
  the enum as a plain VARCHAR with no CHECK constraint, so a value the database
  has never seen is accepted. A new *column* would have had nowhere to go.
* The idempotency key is derived into ``tracking_id`` (already UNIQUE) because
  there is no column to put the caller's key in.
* ``agent_runs`` correlates to a customer by scanning a JSON trace, because there
  is no ``customer_id`` column. That scan matches on parsed arguments, and it
  cannot be indexed.
* An inbound message that answers nothing cannot be represented at all -- there is
  no ``direction`` column, and no way to add one.

None of those are bad code. They are reasonable answers to a constraint that
should not have existed. So the first thing to build is the thing that removes
it: a versioned, ordered, guarded migration step that runs at startup.

Design, deliberately small:

* **One table**, ``schema_migrations``, recording what has been applied and when.
  Not a framework; there is no down-migration, because reverting a schema change
  in production is a decision a human makes with a backup, not a method call.
* **Every migration states its own guard** (``already_applied``) instead of relying
  on the version table alone. A fresh database gets the current schema from
  ``create_all``, so the column is already there and the migration must record
  itself as applied without trying to add it again. Trusting the version table in
  that case would fail on every new install.
* **Applied in order, the whole run in one transaction**, so a failure rolls the
  run back rather than leaving half of it silently done. It is the whole run and
  not one transaction per migration -- stronger than the sequence-stop promise,
  and the version table never claims a schema the database does not have.
* **It reports what it did**, and startup logs it. A migration that runs without
  anybody knowing is how a schema drifts from what people believe it is.

**On a database that is being read and written right now.** The run is safe next
to live traffic, and the reasons are worth stating because they are what makes
*any* of these migrations legal here:

* It runs at startup, **before this process accepts a request** (``init_db`` in
  the lifespan). The readers and writers to worry about are therefore *other*
  processes -- an instance still shutting down, the CLI, a second starter.
* WAL (``journal_mode=WAL``, see ``app/database.py``): readers see the schema as
  it was before the migration's transaction and the new one after it commits.
  There is no intermediate state to read, because the run is one transaction.
* Only ``ALTER TABLE ... ADD COLUMN`` and ``CREATE INDEX`` are used. Both are
  backward compatible: an old process's ORM does not know the new column and
  does not select it, and a DEFAULT covers its inserts. Nothing is dropped,
  renamed or re-typed -- that would need an expand/contract sequence this
  migrator does not attempt.
* Writers in other processes block on the single-writer lock for the duration of
  the transaction, up to ``busy_timeout`` (30s). ``ADD COLUMN`` is metadata-only
  and fast; ``CREATE INDEX`` on a large table is the one statement that can hold
  the lock for a while.

**Two processes migrating at once.** Used to crash the loser: both see the
migration as unapplied, the second one's ``ALTER`` hits "duplicate column" (or
its read-then-write snapshot fails with "database is locked") and startup died
over a race that had already been won by the other process. :func:`run_migrations`
retries the whole run in a *fresh* transaction: the loser's next attempt reads
the version table and the guards, sees the winner's committed work, and skips
cleanly. A race between starters is not a reason to refuse to start.

SQLite allows ``ALTER TABLE ... ADD COLUMN`` and ``CREATE INDEX``; those are what
this needs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

TRACKING_TABLE = "schema_migrations"


@dataclass(frozen=True)
class Migration:
    """One ordered step. ``guard`` decides, ``apply`` does."""

    id: str
    description: str
    #: Statements run in order. Kept as text because a migration describes what
    #: the database looked like at a moment in time; referring to the models
    #: would make it change meaning whenever a model changes.
    statements: list[str] = field(default_factory=list)
    #: True when the database already has this change. A fresh database created
    #: by ``create_all`` is always in this state.
    guard: Callable | None = None

    async def already_applied(self, conn: AsyncConnection) -> bool:
        if self.guard is None:
            return False
        return bool(await self.guard(conn))


async def _has_column(conn: AsyncConnection, table: str, column: str) -> bool:
    rows = await conn.execute(text(f"PRAGMA table_info({table})"))
    return any(row[1] == column for row in rows.fetchall())


async def _column_settled(conn: AsyncConnection, table: str, column: str) -> bool:
    """True when there is nothing for an ADD COLUMN migration to do.

    Two settled states, and both mean "record it, do not run it": the column is
    there (a fresh install, where ``create_all`` produced the current schema),
    or the *table* is not there at all. The second is for databases that predate
    the table -- a migration cannot ALTER a table that does not exist, and the
    table will be created current whenever it is created. Production never sees
    this case (``init_db`` runs ``create_all`` first), but a migration that
    assumes it is a migration that only works on a full install.
    """
    if not await _table_exists(conn, table):
        return True
    return await _has_column(conn, table, column)


async def _table_exists(conn: AsyncConnection, table: str) -> bool:
    row = (
        await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": table},
        )
    ).first()
    return row is not None


async def _has_index(conn: AsyncConnection, name: str) -> bool:
    row = (
        await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name=:name"),
            {"name": name},
        )
    ).first()
    return row is not None


async def _indexes_settled(conn: AsyncConnection, table: str, names: list[str]) -> bool:
    """True when there is nothing for this CREATE INDEX migration to do.

    Two settled states, the same pair ``_column_settled`` reasons about: the
    indexes are there (a fresh install, where the model's ``index=True`` had
    ``create_all`` build them), or the *table* is not there at all -- a
    migration cannot index a table that does not exist, and the table will be
    created with its indexes whenever it is created. Production never sees the
    second case (``init_db`` runs ``create_all`` first), but a migration that
    assumes a full install is a migration that only works on one.
    """
    if not await _table_exists(conn, table):
        return True
    for name in names:
        if not await _has_index(conn, name):
            return False
    return True


async def _idempotency_settled(conn: AsyncConnection) -> bool:
    """Both halves of 0007, because the column without the index is not enough.

    A unique index is the thing that makes concurrent retries safe; a column on
    its own only makes them look recorded. A fresh install has the column from
    ``create_all`` and the index from the model, so this is true; an upgraded
    database has neither until the migration runs.
    """
    column = await _column_settled(conn, "agent_runs", "idempotency_key")
    index = await _indexes_settled(
        conn, "agent_runs", ["ix_agent_runs_idempotency_key"]
    )
    return column and index


async def _stop_code_settled(conn: AsyncConnection) -> bool:
    """Column and index, for the same reason as 0007.

    The index is what makes "how many runs stopped for each reason this week"
    a lookup rather than a scan; a column without it would answer the question
    slowly enough that nobody would ask it.
    """
    column = await _column_settled(conn, "agent_runs", "stop_code")
    index = await _indexes_settled(conn, "agent_runs", ["ix_agent_runs_stop_code"])
    return column and index


# --------------------------------------------------------------------------
# The migrations, oldest first
# --------------------------------------------------------------------------

MIGRATIONS: list[Migration] = [
    Migration(
        id="0001_agent_runs_customer_id",
        description=(
            "give agent_runs a customer reference, so a run can be found from the "
            "customer it was about instead of by scanning its trace"
        ),
        statements=[
            "ALTER TABLE agent_runs ADD COLUMN customer_id INTEGER",
            "CREATE INDEX IF NOT EXISTS ix_agent_runs_customer_id "
            "ON agent_runs (customer_id)",
        ],
        guard=lambda conn: _has_column(conn, "agent_runs", "customer_id"),
    ),
    Migration(
        id="0002_agent_runs_cached_prompt_tokens",
        description=(
            "record how much of a run's prompt the provider served from its "
            "prefix cache, so the run's token count can be turned into a price"
        ),
        statements=[
            "ALTER TABLE agent_runs ADD COLUMN cached_prompt_tokens INTEGER DEFAULT 0",
        ],
        guard=lambda conn: _has_column(conn, "agent_runs", "cached_prompt_tokens"),
    ),
    Migration(
        id="0003_email_logs_simulated",
        description=(
            "record whether a 'sent' row ever left the process. Without it a "
            "simulated send (no SMTP credentials) is indistinguishable from a "
            "real one, and a missing email cannot even be triaged: the first "
            "question -- was it ever really sent? -- has no answer in the data"
        ),
        statements=[
            # INTEGER, not BOOLEAN: SQLite has no boolean type, and the values
            # that matter are the 0/1 that SQLAlchemy's Boolean reads back.
            "ALTER TABLE email_logs ADD COLUMN simulated INTEGER DEFAULT 0",
        ],
        guard=lambda conn: _column_settled(conn, "email_logs", "simulated"),
    ),
    Migration(
        id="0004_email_logs_hot_indexes",
        description=(
            "index the email_logs columns every hot query filters or sorts by. "
            "Before this, measured on a synthetic book of 10k customers / 400k "
            "email rows, the log listing (ORDER BY created_at DESC LIMIT 100) "
            "took 774 ms because the sort had to walk the whole table, and the "
            "status counts took 155 ms for the same reason"
        ),
        statements=[
            # The plan, the history view and the debug view all ask "this
            # customer's mail" -- one index serves every one of them.
            "CREATE INDEX IF NOT EXISTS ix_email_logs_customer_id "
            "ON email_logs (customer_id)",
            # The listing sorts newest-first over the whole table.
            "CREATE INDEX IF NOT EXISTS ix_email_logs_created_at "
            "ON email_logs (created_at)",
            # The claim scans WHERE status='pending'; the stats count by status.
            "CREATE INDEX IF NOT EXISTS ix_email_logs_status "
            "ON email_logs (status)",
        ],
        guard=lambda conn: _indexes_settled(
            conn,
            "email_logs",
            ["ix_email_logs_customer_id", "ix_email_logs_created_at", "ix_email_logs_status"],
        ),
    ),
    Migration(
        id="0005_customers_stage_index",
        description=(
            "index customers.stage. Every plan, dashboard and churn query "
            "filters by it and there was no index on it anywhere in the models"
        ),
        statements=[
            "CREATE INDEX IF NOT EXISTS ix_customers_stage ON customers (stage)",
        ],
        guard=lambda conn: _indexes_settled(conn, "customers", ["ix_customers_stage"]),
    ),
    Migration(
        id="0006_agent_runs_prompt_version",
        description=(
            "record which system prompt produced a run. The trace says what the "
            "model did; nothing said what it was told. Without the fingerprint a "
            "change in output cannot be attributed to a prompt edit rather than "
            "to the model or the data, and a run produced by instructions that "
            "are no longer in the tree looks exactly like one produced today"
        ),
        statements=[
            "ALTER TABLE agent_runs ADD COLUMN prompt_version VARCHAR(32)",
        ],
        guard=lambda conn: _has_column(conn, "agent_runs", "prompt_version"),
    ),
    Migration(
        id="0007_agent_runs_idempotency_key",
        description=(
            "let a caller retry 'start a run' without paying for it twice. A run "
            "costs money and writes to the book a human is reading, so a client "
            "that times out must be able to ask again with the same key and get "
            "the same run back instead of a second one"
        ),
        statements=[
            "ALTER TABLE agent_runs ADD COLUMN idempotency_key VARCHAR(120)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_agent_runs_idempotency_key "
            "ON agent_runs (idempotency_key)",
        ],
        guard=_idempotency_settled,
    ),
    Migration(
        id="0008_agent_runs_stop_code",
        description=(
            "record why a run stopped as a stable code, not only as a sentence. "
            "Three different 'truncated' causes -- time, rounds, tokens -- shared "
            "one status and three prose strings, so a metric could not separate "
            "them and a dashboard had to show free text. The sentence stays: it "
            "is what a human reads. The code is what a query groups by"
        ),
        statements=[
            "ALTER TABLE agent_runs ADD COLUMN stop_code VARCHAR(32)",
            "CREATE INDEX IF NOT EXISTS ix_agent_runs_stop_code "
            "ON agent_runs (stop_code)",
            # Backfill only the states whose sentences this codebase wrote. Any
            # other historical wording is left as legacy_unknown rather than
            # guessed into a bucket it may not belong to.
            "UPDATE agent_runs SET stop_code = CASE "
            "  WHEN stop_reason = 'final answer' THEN 'final_answer' "
            "  WHEN stop_reason = 'time budget exceeded' THEN 'time_budget' "
            "  WHEN stop_reason = 'round budget exhausted' THEN 'round_budget' "
            "  WHEN stop_reason = 'token budget exhausted' THEN 'token_budget' "
            "  WHEN stop_reason = 'model produced no usable tool calls and no "
            "substantive answer' THEN 'stalled' "
            "  WHEN stop_reason = 'abandoned' THEN 'abandoned' "
            "  WHEN status = 'error' THEN 'error' "
            "  ELSE 'legacy_unknown' "
            "END "
            "WHERE stop_code IS NULL AND stop_reason IS NOT NULL",
        ],
        guard=_stop_code_settled,
    ),
]


async def _ensure_tracking_table(conn: AsyncConnection) -> None:
    await conn.execute(text(
        f"CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} ("
        "  id VARCHAR(120) PRIMARY KEY,"
        "  description TEXT,"
        "  applied_at DATETIME"
        ")"
    ))


async def applied_ids(conn: AsyncConnection) -> set[str]:
    rows = await conn.execute(text(f"SELECT id FROM {TRACKING_TABLE}"))
    return {row[0] for row in rows.fetchall()}


async def apply_migrations(conn: AsyncConnection) -> dict:
    """Bring an existing database up to the current schema.

    Returns a report rather than only logging: ``applied``, ``already_present``
    (guard said yes, so it was recorded without running) and ``pending`` (not
    applied, and the guard could not confirm it -- which for this list means a
    failure, because the sequence stops).

    Runs on a connection the caller owns. Prefer :func:`run_migrations`, which
    owns its transaction and survives a concurrent migrator; this is the part
    that does the work, kept separate so tests can drive it directly.
    """
    await _ensure_tracking_table(conn)
    done = await applied_ids(conn)

    report = {"applied": [], "already_present": [], "pending": []}

    for migration in MIGRATIONS:
        if migration.id in done:
            continue

        if await migration.already_applied(conn):
            # The database already looks like this -- a fresh install, where
            # create_all produced the current schema. Record it so the *next*
            # run does not have to ask again, and say so rather than pretending
            # to have migrated something.
            report["already_present"].append(migration.id)
        else:
            for statement in migration.statements:
                await conn.execute(text(statement))
            report["applied"].append(migration.id)
            logger.info("migration applied: %s -- %s", migration.id, migration.description)

        await conn.execute(
            text(
                f"INSERT INTO {TRACKING_TABLE} (id, description, applied_at) "
                "VALUES (:id, :description, :applied_at)"
            ),
            {
                "id": migration.id,
                "description": migration.description,
                "applied_at": datetime.utcnow(),
            },
        )

    report["pending"] = [
        m.id for m in MIGRATIONS if m.id not in done and m.id not in report["applied"]
        and m.id not in report["already_present"]
    ]
    return report


#: A concurrent migrator shows up as one of these. The first is the loser's
#: read-then-write on a WAL snapshot the winner has already moved past; the
#: second is its ``ALTER`` arriving after the winner's identical one. Neither
#: means the migration is bad -- both mean someone else did it first.
_MIGRATION_RACE_MARKERS = (
    "database is locked",
    "database table is locked",
    "duplicate column",
    "already exists",
    "UNIQUE constraint failed",
)


def _is_migration_race(exc: BaseException) -> bool:
    message = str(exc)
    return any(marker.lower() in message.lower() for marker in _MIGRATION_RACE_MARKERS)


async def run_migrations(engine, attempts: int = 3) -> dict:
    """Apply the pending migrations, surviving a concurrent migrator.

    Two processes starting against the same database -- a restart that overlaps,
    a CLI while the server boots -- both find the migrations unapplied and both
    try to run them. The winner commits; the loser's statements fail against the
    winner's schema (or its snapshot goes stale). The loser's failure is not a
    startup failure: the whole run is rolled back and retried in a *fresh*
    transaction, where the version table and the guards now see the winner's
    committed work and every migration is skipped as already present.

    A genuine migration error is not retried: "THIS IS NOT SQL" is not a race,
    and pretending it is would just hide the bug three times. Anything that is
    not recognisably a race raises on the first attempt.
    """
    from sqlalchemy.exc import OperationalError, IntegrityError

    for attempt in range(1, attempts + 1):
        try:
            async with engine.begin() as conn:
                return await apply_migrations(conn)
        except (OperationalError, IntegrityError) as exc:
            if attempt >= attempts or not _is_migration_race(exc):
                raise
            logger.warning(
                "migration attempt %d/%d lost to a concurrent migrator (%s); "
                "retrying against what it committed",
                attempt, attempts, exc,
            )
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


async def schema_status(conn: AsyncConnection) -> dict:
    """What the database thinks its schema is. For debugging and for tests."""
    await _ensure_tracking_table(conn)
    known = {m.id for m in MIGRATIONS}
    done = await applied_ids(conn)
    return {
        "known": [m.id for m in MIGRATIONS],
        "applied": sorted(done & known),
        "unapplied": sorted(known - done),
        "unknown_in_database": sorted(done - known),
    }
