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
* **Applied in order, in one transaction per migration**, so a failure stops the
  sequence rather than leaving half of it silently done.
* **It reports what it did**, and startup logs it. A migration that runs without
  anybody knowing is how a schema drifts from what people believe it is.

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


async def _table_exists(conn: AsyncConnection, table: str) -> bool:
    row = (
        await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": table},
        )
    ).first()
    return row is not None


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
