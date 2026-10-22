"""Schema changes on a database that already exists.

The decision this file is about: the schema used to be
``Base.metadata.create_all`` and nothing else. Tables are created if missing, and
an existing database is never touched again -- which is fine on day one and
wrong forever after. It shaped a lot of this codebase:

* ``EmailStatus.SENDING`` was only safe because SQLite stores the enum as a
  VARCHAR with no CHECK constraint. A new *column* had nowhere to go.
* The idempotency key is derived into ``tracking_id`` because there is no column
  for the caller's key.
* ``agent_runs`` correlated to a customer by scanning a JSON trace, because there
  was no ``customer_id`` column -- unindexable, and blind to a run that named a
  customer only in prose.

So the tests here are mostly about the awkward case: a database that is *already
in use*. A migration that only works on an empty file is not a migration.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.migrations import MIGRATIONS, apply_migrations, schema_status

LEGACY_AGENT_RUNS = """
CREATE TABLE agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id VARCHAR(64) NOT NULL UNIQUE,
    goal TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    stop_reason VARCHAR(300),
    error TEXT,
    final_output TEXT,
    rounds INTEGER DEFAULT 0,
    tool_calls INTEGER DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    elapsed INTEGER DEFAULT 0,
    trace TEXT,
    created_at DATETIME,
    finished_at DATETIME
)
"""


@pytest.fixture
def scratch(tmp_path):
    """A private database file, so a schema test cannot disturb the suite's."""
    import asyncio

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'schema.db').as_posix()}"
    )

    class Scratch:
        def __init__(self, engine):
            #: The engine itself, for callers that need to own the transaction
            #: (``run_migrations`` does, so it can retry in a fresh one).
            self.engine = engine

        async def run(self, coro_factory):
            async with self.engine.begin() as conn:
                return await coro_factory(conn)

        async def dispose(self):
            await self.engine.dispose()

    handle = Scratch(engine)
    yield handle
    asyncio.run(handle.dispose())


LEGACY_AGENT_CONVERSATIONS = """
CREATE TABLE agent_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id VARCHAR(64) NOT NULL UNIQUE,
    title VARCHAR(120) NOT NULL DEFAULT '',
    customer_id INTEGER,
    created_at DATETIME,
    updated_at DATETIME
)
"""


def as_legacy_database(scratch):
    """Build the tables the way they looked before the migrations ran.

    ``agent_conversations`` is included even though most migrations here only
    touch ``agent_runs``: a real database of that age has it, because
    ``create_all`` had been adding it on every start. A fixture that omits it
    would test a shape the product never produces, and would fail the first
    migration that alters the other table.
    """
    import asyncio

    async def build(conn):
        await conn.execute(text(LEGACY_AGENT_RUNS))
        await conn.execute(text(LEGACY_AGENT_CONVERSATIONS))
        await conn.execute(
            text(
                "INSERT INTO agent_runs (run_id, goal, status, created_at) "
                "VALUES (:run_id, :goal, 'succeeded', :created_at)"
            ),
            {"run_id": uuid.uuid4().hex, "goal": "an old run",
             "created_at": datetime.utcnow()},
        )

    asyncio.run(scratch.run(build))


def tables(scratch):
    import asyncio

    async def read(conn):
        rows = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
        )
        return [row[0] for row in rows.fetchall()]

    return asyncio.run(scratch.run(read))


def columns(scratch, table):
    import asyncio

    async def read(conn):
        rows = await conn.execute(text(f"PRAGMA table_info({table})"))
        return [row[1] for row in rows.fetchall()]

    return asyncio.run(scratch.run(read))


def indexes(scratch, table):
    import asyncio

    async def read(conn):
        rows = await conn.execute(text(f"PRAGMA index_list({table})"))
        return [row[1] for row in rows.fetchall()]

    return asyncio.run(scratch.run(read))


# --------------------------------------------------------------------------
# The real case: a database that has been running
# --------------------------------------------------------------------------

def test_a_legacy_database_gains_the_column(scratch):
    as_legacy_database(scratch)
    assert "customer_id" not in columns(scratch, "agent_runs")

    import asyncio

    report = asyncio.run(scratch.run(apply_migrations))

    assert "0001_agent_runs_customer_id" in report["applied"], report
    assert "customer_id" in columns(scratch, "agent_runs")
    assert any("customer_id" in name for name in indexes(scratch, "agent_runs")), (
        "the column is only useful if finding runs by it does not scan the table"
    )


def test_a_legacy_database_gains_the_prompt_identity_column(scratch):
    """Old runs cannot say what they were told; new ones must.

    The column is nullable on purpose: a row written before it existed has no
    prompt to name, and inventing one would make a legacy run look reproducible
    when it is not.
    """
    as_legacy_database(scratch)
    assert "prompt_version" not in columns(scratch, "agent_runs")

    import asyncio

    report = asyncio.run(scratch.run(apply_migrations))

    assert "0006_agent_runs_prompt_version" in report["applied"], report
    assert "prompt_version" in columns(scratch, "agent_runs")


def test_a_legacy_database_gains_a_place_to_remember_an_idempotency_key(scratch):
    """A retry needs somewhere to point, including on a book that predates it."""
    import asyncio

    as_legacy_database(scratch)
    assert "idempotency_key" not in columns(scratch, "agent_runs")

    report = asyncio.run(scratch.run(apply_migrations))

    assert "0007_agent_runs_idempotency_key" in report["applied"], report
    assert "idempotency_key" in columns(scratch, "agent_runs")

    async def indexes(conn):
        rows = await conn.execute(text("PRAGMA index_list(agent_runs)"))
        return rows.fetchall()

    names = [row[1] for row in asyncio.run(scratch.run(indexes))]
    assert "ix_agent_runs_idempotency_key" in names, (
        "the column without the unique index cannot stop a racing duplicate"
    )


def test_a_legacy_database_gains_a_conversation_reference(scratch):
    """Old runs were independent; new ones can belong to a thread.

    No backfill: inventing a conversation for a legacy run would fabricate a
    history the trace cannot support.
    """
    import asyncio

    as_legacy_database(scratch)
    assert "conversation_id" not in columns(scratch, "agent_runs")

    report = asyncio.run(scratch.run(apply_migrations))

    assert "0011_agent_runs_conversation_id" in report["applied"], report
    assert "conversation_id" in columns(scratch, "agent_runs")
    assert any("conversation_id" in name for name in indexes(scratch, "agent_runs")), (
        "the conversation view reads by this column; without the index it scans"
    )


def test_a_legacy_database_learns_which_model_ran(scratch):
    """A token count with no model name cannot be priced."""
    import asyncio

    as_legacy_database(scratch)
    assert "model" not in columns(scratch, "agent_runs")

    report = asyncio.run(scratch.run(apply_migrations))

    assert "0012_agent_runs_model" in report["applied"], report
    assert "model" in columns(scratch, "agent_runs")


def test_a_legacy_database_learns_what_a_thread_concluded(scratch):
    """A thread used to die with its last message; the digest is what survives it."""
    import asyncio

    as_legacy_database(scratch)
    assert "summary" not in columns(scratch, "agent_conversations")
    assert "agent_memories" not in tables(scratch)

    report = asyncio.run(scratch.run(apply_migrations))

    assert "0014_cross_session_memory" in report["applied"], report
    assert {"summary", "summary_turns", "summarized_at"} <= set(
        columns(scratch, "agent_conversations")
    )
    assert "agent_memories" in tables(scratch)
    assert "ix_agent_memories_key" in indexes(scratch, "agent_memories"), (
        "the unique index is what makes a repeated key an update, not a duplicate"
    )


def test_a_legacy_thread_starts_with_nothing_summarised(scratch):
    """DEFAULT 0, not NULL: "nothing summarised yet" has to compare as a number."""
    import asyncio

    as_legacy_database(scratch)

    async def seed(conn):
        await conn.execute(
            text(
                "INSERT INTO agent_conversations "
                "(conversation_id, title, created_at, updated_at) "
                "VALUES ('legacy-thread', 'old thread', :now, :now)"
            ),
            {"now": datetime.utcnow()},
        )

    asyncio.run(scratch.run(seed))
    asyncio.run(scratch.run(apply_migrations))

    async def read(conn):
        return (
            await conn.execute(
                text(
                    "SELECT summary, summary_turns FROM agent_conversations "
                    "WHERE conversation_id = 'legacy-thread'"
                )
            )
        ).fetchone()

    summary, turns = asyncio.run(scratch.run(read))
    assert summary is None, "a legacy thread has no note to invent"
    assert turns == 0, "and must not look as if it had already been summarised"


def test_the_same_note_key_cannot_be_stored_twice(scratch):
    """The key is the identity. Without the unique index, "remember" would
    silently accumulate two rows that disagree and the next thread would be told
    both."""
    import asyncio

    as_legacy_database(scratch)
    asyncio.run(scratch.run(apply_migrations))

    async def double_insert(conn):
        for _ in range(2):
            await conn.execute(
                text("INSERT INTO agent_memories (key, value) VALUES ('k', 'v')")
            )

    with pytest.raises(Exception):
        asyncio.run(scratch.run(double_insert))


def test_legacy_stop_reasons_are_backfilled_with_a_code(scratch):
    """The sentence stays; the code is derived once, from what we actually wrote.

    A migration that only adds the column would leave every historical run
    ungroupable. Guessing arbitrary prose into a bucket would be worse than
    saying ``legacy_unknown``.
    """
    import asyncio

    as_legacy_database(scratch)

    async def seed(conn):
        await conn.execute(text(
            "INSERT INTO agent_runs (run_id, goal, status, stop_reason) VALUES "
            "('r-final', 'g', 'succeeded', 'final answer'), "
            "('r-token', 'g', 'truncated', 'token budget exhausted'), "
            "('r-weird', 'g', 'truncated', 'a sentence from an older build')"
        ))

    asyncio.run(scratch.run(seed))
    report = asyncio.run(scratch.run(apply_migrations))

    assert "0008_agent_runs_stop_code" in report["applied"], report

    async def read(conn):
        rows = await conn.execute(text(
            "SELECT run_id, stop_code FROM agent_runs ORDER BY run_id"
        ))
        return rows.fetchall()

    codes = dict(asyncio.run(scratch.run(read)))
    assert codes["r-final"] == "final_answer"
    assert codes["r-token"] == "token_budget"
    assert codes["r-weird"] == "legacy_unknown"


def test_a_legacy_database_gains_the_idempotency_table(scratch):
    """Without the unique index the table accepts the duplicate it exists to stop."""
    import asyncio

    as_legacy_database(scratch)
    report = asyncio.run(scratch.run(apply_migrations))

    assert "0009_idempotency_keys" in report["applied"], report

    async def inspect(conn):
        table = await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='idempotency_keys'"
        ))
        index = await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='uq_idempotency_scope_key'"
        ))
        return table.fetchone(), index.fetchone()

    table, index = asyncio.run(scratch.run(inspect))
    assert table is not None
    assert index is not None, "the unique (scope, key) index is the guarantee"


def test_a_legacy_row_survives_the_migration(scratch):
    """The rows that were already there are the ones this exists for."""
    import asyncio

    as_legacy_database(scratch)
    asyncio.run(scratch.run(apply_migrations))

    async def read(conn):
        rows = await conn.execute(
            text("SELECT goal, customer_id, prompt_version FROM agent_runs")
        )
        return rows.fetchall()

    rows = asyncio.run(scratch.run(read))

    assert len(rows) == 1, "the migration lost a row"
    assert rows[0][0] == "an old run"
    assert rows[0][1] is None, (
        "a legacy row has no customer to claim; inventing one would be a lie"
    )
    assert rows[0][2] is None, (
        "a legacy run cannot name the prompt it ran under; filling in a default "
        "would make it look reproducible when it is not"
    )


def test_migrating_twice_changes_nothing(scratch):
    import asyncio

    as_legacy_database(scratch)
    first = asyncio.run(scratch.run(apply_migrations))
    second = asyncio.run(scratch.run(apply_migrations))

    assert first["applied"], first
    assert second["applied"] == [], "a migration ran twice"
    assert second["already_present"] == []
    assert second["pending"] == []


# --------------------------------------------------------------------------
# The fresh database, which must not break
# --------------------------------------------------------------------------

def test_a_fresh_database_records_the_migration_without_running_it(scratch):
    """``create_all`` already produced the current schema.

    The guard exists for this: a brand-new install has the column before any
    migration runs, so trusting the version table alone would try to add a column
    that is already there and fail on every first start.
    """
    import asyncio

    from app.database import Base
    import app.models  # noqa: F401

    async def create(conn):
        await conn.run_sync(Base.metadata.create_all)

    asyncio.run(scratch.run(create))
    assert "customer_id" in columns(scratch, "agent_runs")

    report = asyncio.run(scratch.run(apply_migrations))

    assert report["applied"] == []
    assert set(report["already_present"]) == {m.id for m in MIGRATIONS}, report
    assert report["pending"] == []

    status = asyncio.run(scratch.run(schema_status))
    assert status["unapplied"] == []
    assert set(status["applied"]) == {m.id for m in MIGRATIONS}


def test_the_status_reports_what_the_database_thinks_it_is(scratch):
    import asyncio

    as_legacy_database(scratch)
    before = asyncio.run(scratch.run(schema_status))
    assert set(before["unapplied"]) == {m.id for m in MIGRATIONS}

    asyncio.run(scratch.run(apply_migrations))
    after = asyncio.run(scratch.run(schema_status))
    assert after["unapplied"] == []
    assert set(after["applied"]) == {m.id for m in MIGRATIONS}


# --------------------------------------------------------------------------
# The mechanism's own properties
# --------------------------------------------------------------------------

def test_a_failing_migration_stops_the_sequence(scratch):
    """Half-applied is worse than not applied: it is invisible.

    A migration that raises must not be recorded, and the ones behind it must
    not run -- otherwise the version table claims a schema the database does not
    have, and every later run believes it.
    """
    import asyncio

    from app import migrations

    broken = [
        migrations.Migration(
            id="9001_will_fail",
            description="deliberately invalid",
            statements=["ALTER TABLE agent_runs ADD COLUMN nope INTEGER",
                        "THIS IS NOT SQL"],
            guard=None,
        ),
        migrations.Migration(
            id="9002_must_not_run",
            description="behind the failure",
            statements=["ALTER TABLE agent_runs ADD COLUMN later INTEGER"],
            guard=None,
        ),
    ]

    as_legacy_database(scratch)
    original = migrations.MIGRATIONS
    migrations.MIGRATIONS = broken
    try:
        with pytest.raises(Exception):
            asyncio.run(scratch.run(apply_migrations))
    finally:
        migrations.MIGRATIONS = original

    assert "later" not in columns(scratch, "agent_runs"), (
        "a migration behind a failure ran anyway"
    )


def test_the_migration_list_is_ordered_and_identified_once():
    """Order is the whole contract, and ids are what the version table trusts."""
    ids = [migration.id for migration in MIGRATIONS]

    assert ids == sorted(ids), f"migrations are not in order: {ids}"
    assert len(ids) == len(set(ids)), f"duplicate migration id: {ids}"
    for migration in MIGRATIONS:
        assert migration.guard is not None, (
            f"{migration.id} has no guard; a fresh database would try to apply it"
        )
        assert migration.description, f"{migration.id} does not say what it does"


def test_a_run_naming_a_customer_that_does_not_exist_is_refused(client):
    """SQLite will not enforce the foreign key unless it is switched on.

    So a wrong id would be written and then quietly match nothing: a run recorded
    as being about a customer who does not exist, which is worse than a rejected
    request because it looks like data.
    """
    import app.routers.agent_runs as routes

    response = client.post(
        "/api/v1/agent/run",
        json={"goal": "review this account", "customer_id": 987654},
    )

    # 503 first if no model is configured; the point is that it is not a 202/200
    # that quietly recorded a run against a phantom customer.
    assert response.status_code in (404, 503), response.text
    if response.status_code == 404:
        assert "no customer with id 987654" in response.json()["detail"]


def test_the_customer_reference_reaches_the_run_row(client, db):
    """End to end: the API field, the run, the row, the indexed lookup.

    The agent is mocked at the model boundary -- the point is not that the agent
    works, it is that naming a customer on a run is honoured all the way down and
    can be read back **by the column**, without searching a trace.

    Uses ``wait=false`` on purpose: that is the path that writes the row up
    front, so the row exists whatever the fake run returns. With ``wait=true``
    nothing is persisted by the fake and the assertion could quietly not run.
    """
    import asyncio
    import uuid as uuidlib

    from sqlalchemy import select

    from app.agent.loop import AgentRun as RunResult
    from app.agent.service import AgentResult
    import app.routers.agent_runs as routes
    from app.models.agent_run import AgentRun
    from app.models.customer import Customer

    suffix = uuidlib.uuid4().hex[:8]
    customer = Customer(company_name=f"Migrated {suffix}", stage="completed",
                        source="manual")
    db.add(customer)
    asyncio.run(db.commit())
    asyncio.run(db.refresh(customer))

    async def fake_execute(goal, *, customer_id=None, **kwargs):
        run = RunResult(goal=goal, customer_id=customer_id, status="succeeded",
                        stop_reason="final answer", final_output="done")
        return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")

    original_configured, original_execute = routes.is_configured, routes.execute
    routes.is_configured = lambda: True
    routes.execute = fake_execute
    try:
        response = client.post(
            "/api/v1/agent/run",
            json={"goal": "review this account", "customer_id": customer.id},
        )
        assert response.status_code in (200, 202), response.text
        run_id = response.json()["run_id"]

        # Let the background run settle, then read the row directly.
        for _ in range(50):
            async def read():
                return (
                    await db.execute(
                        select(AgentRun).where(AgentRun.run_id == run_id)
                    )
                ).scalar_one_or_none()

            row = asyncio.run(read())
            if row is not None and row.status != "running":
                break
            import time
            time.sleep(0.02)
    finally:
        routes.is_configured, routes.execute = original_configured, original_execute

    assert row is not None, "the run row was never written"
    assert row.customer_id == customer.id, (
        "the customer named on the request never reached the row"
    )

    view = client.get(f"/api/v1/customers/{customer.id}/debug").json()
    found = [r for r in view["runs"] if r["run_id"] == run_id]
    assert found, "the debug view could not find the run"
    assert found[0]["matched_by"] == "customer_id column"

    # Clean up: this customer was created for the test.
    asyncio.run(db.delete(customer))
    asyncio.run(db.commit())


def test_the_tracking_table_is_not_a_model():
    """It is created by the runner, not by ``create_all``.

    If it were a model, a fresh database would get it from ``create_all`` and an
    existing one would not -- and the runner has to work on the existing one.
    """
    from app.database import Base
    from app.migrations import TRACKING_TABLE

    assert TRACKING_TABLE not in Base.metadata.tables


# --------------------------------------------------------------------------
# Two processes migrating at once
# --------------------------------------------------------------------------

def _dbapi_error(message: str):
    """What the loser's statements actually raise, in either flavour.

    SQLAlchemy wraps the driver's error; the message is what tells the two
    apart. ``OperationalError`` covers "duplicate column name" and "database is
    locked"; ``IntegrityError`` covers two processes recording the same
    migration id in ``schema_migrations``.
    """
    from sqlalchemy.exc import OperationalError

    return OperationalError("ALTER TABLE ...", None, Exception(message))


def test_a_loser_against_a_concurrent_migrator_retries_instead_of_refusing_to_start(
    scratch, monkeypatch
):
    """The race used to be a startup crash, not a race.

    Two starters find the migrations unapplied and both run them. The loser's
    statements fail against the winner's schema ("duplicate column name") or
    its snapshot goes stale ("database is locked"). That is the winner having
    done the work -- not a bad migration -- so the run is retried in a fresh
    transaction and reports the migrations as already present.
    """
    import asyncio

    from app import migrations

    as_legacy_database(scratch)

    real = migrations.apply_migrations
    calls = {"n": 0}

    async def lose_once_then_recover(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _dbapi_error("duplicate column name: customer_id")
        return await real(conn)

    monkeypatch.setattr(migrations, "apply_migrations", lose_once_then_recover)

    report = asyncio.run(migrations.run_migrations(scratch.engine))

    assert calls["n"] == 2, "the loser must retry, not give up"
    assert report["applied"], report
    assert "customer_id" in columns(scratch, "agent_runs")


def test_a_genuine_migration_error_is_not_retried(scratch, monkeypatch):
    """Retrying a bug three times hides the bug three times.

    Only a recognisable race is retried. A syntax error is not a race, and the
    error must surface on the first attempt, pointing at the migration.
    """
    import asyncio

    from app import migrations

    as_legacy_database(scratch)
    calls = {"n": 0}

    async def explode(conn):
        calls["n"] += 1
        raise _dbapi_error('near "THIS": syntax error')

    monkeypatch.setattr(migrations, "apply_migrations", explode)

    with pytest.raises(Exception):
        asyncio.run(migrations.run_migrations(scratch.engine))

    assert calls["n"] == 1, "a syntax error is not a race; it must not be retried"


def test_two_starters_on_one_database_both_survive(tmp_path):
    """The real thing: two engines, one database file, one migration run each.

    Whoever wins writes the columns and the version rows; the loser's retry
    sees that and records them as already present. Neither starter may die
    over it -- that is the whole point of the retry. (The version table's
    primary key is what turns the loser's record attempt into the very
    IntegrityError the retry exists for.)
    """
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.migrations import run_migrations

    db_file = (tmp_path / "shared.db").as_posix()
    engine_a = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    engine_b = create_async_engine(f"sqlite+aiosqlite:///{db_file}")

    async def legacy():
        async with engine_a.begin() as conn:
            await conn.execute(text(LEGACY_AGENT_RUNS))

    async def both():
        return await asyncio.gather(
            run_migrations(engine_a),
            run_migrations(engine_b),
            return_exceptions=True,
        )

    try:
        asyncio.run(legacy())
        results = asyncio.run(both())

        for r in results:
            assert not isinstance(r, BaseException), f"a starter died over the race: {r}"

        # The run is one transaction: one starter applied what was pending,
        # the other found it done. Both outcomes must be in the reports.
        applied_counts = sorted(len(r["applied"]) for r in results)
        assert applied_counts[0] == 0, f"the loser applied nothing: {applied_counts}"
        assert applied_counts[1] > 0, f"someone applied the pending work: {applied_counts}"

        async def cols():
            async with engine_a.begin() as conn:
                rows = await conn.execute(text("PRAGMA table_info(agent_runs)"))
                return [row[1] for row in rows.fetchall()]

        names = asyncio.run(cols())
        assert "customer_id" in names
        assert "cached_prompt_tokens" in names

        async def tracking():
            async with engine_a.begin() as conn:
                rows = await conn.execute(
                    text("SELECT id FROM schema_migrations ORDER BY id")
                )
                return [row[0] for row in rows.fetchall()]

        recorded = asyncio.run(tracking())
        assert len(recorded) == len(set(recorded)), "a migration was recorded twice"
        assert set(recorded) == {m.id for m in MIGRATIONS}, recorded
    finally:
        asyncio.run(engine_a.dispose())
        asyncio.run(engine_b.dispose())


#: ``email_logs`` as it stood before 0010, copied from a real database's
#: ``sqlite_master`` (the foreign keys are dropped: a scratch database has no
#: ``customers`` table and the pragma that enforces them is off by default).
LEGACY_EMAIL_LOGS = """
CREATE TABLE email_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER,
    template_id INTEGER,
    to_email VARCHAR(200) NOT NULL,
    to_name VARCHAR(200),
    subject VARCHAR(500) NOT NULL,
    body TEXT NOT NULL,
    status VARCHAR(9),
    tracking_id VARCHAR(100),
    opened_at DATETIME,
    open_count INTEGER,
    replied_at DATETIME,
    error_message TEXT,
    created_at DATETIME,
    sent_at DATETIME,
    simulated INTEGER DEFAULT 0
)
"""


def test_legacy_mail_stays_unattributed_after_the_provenance_migration(scratch):
    """A backfill here would be a guess counted as a measurement.

    Mail sent before the column existed has no suggestion behind it in the
    record. Filling it with the most likely kind would make the by-kind table
    look complete, and every rate in it would then be computed over rows whose
    provenance was invented.
    """
    import asyncio

    as_legacy_database(scratch)

    async def build(conn):
        await conn.execute(text(LEGACY_EMAIL_LOGS))
        await conn.execute(text(
            "INSERT INTO email_logs (to_email, subject, body, status) VALUES "
            "('old@example.test', 's', 'b', 'SENT')"
        ))

    asyncio.run(scratch.run(build))
    assert "suggestion_kind" not in columns(scratch, "email_logs")

    report = asyncio.run(scratch.run(apply_migrations))

    assert "0010_email_logs_suggestion_provenance" in report["applied"], report
    assert {"suggestion_kind", "suggestion_attempt"} <= set(columns(scratch, "email_logs"))

    async def read(conn):
        rows = await conn.execute(text(
            "SELECT suggestion_kind, suggestion_attempt FROM email_logs"
        ))
        return rows.fetchall()

    assert asyncio.run(scratch.run(read)) == [(None, None)], (
        "an old row was given a provenance it never had"
    )

    names = indexes(scratch, "email_logs")
    assert "ix_email_logs_suggestion_kind" in names, (
        "without the index, 'reply rate by kind' is a scan of the whole table"
    )


#: ``email_logs`` as it stood before 0010, copied from a real database's
#: ``sqlite_master`` (the foreign keys are dropped: a scratch database has no
#: ``customers`` table and the pragma that enforces them is off by default).
LEGACY_EMAIL_LOGS = """
CREATE TABLE email_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER,
    template_id INTEGER,
    to_email VARCHAR(200) NOT NULL,
    to_name VARCHAR(200),
    subject VARCHAR(500) NOT NULL,
    body TEXT NOT NULL,
    status VARCHAR(9),
    tracking_id VARCHAR(100),
    opened_at DATETIME,
    open_count INTEGER,
    replied_at DATETIME,
    error_message TEXT,
    created_at DATETIME,
    sent_at DATETIME,
    simulated INTEGER DEFAULT 0
)
"""


def test_legacy_mail_stays_unattributed_after_the_provenance_migration(scratch):
    """A backfill here would be a guess counted as a measurement.

    Mail sent before the column existed has no suggestion behind it in the
    record. Filling it with the most likely kind would make the by-kind table
    look complete, and every rate in it would then be computed over rows whose
    provenance was invented.
    """
    import asyncio

    as_legacy_database(scratch)

    async def build(conn):
        await conn.execute(text(LEGACY_EMAIL_LOGS))
        await conn.execute(text(
            "INSERT INTO email_logs (to_email, subject, body, status) VALUES "
            "('old@example.test', 's', 'b', 'SENT')"
        ))

    asyncio.run(scratch.run(build))
    assert "suggestion_kind" not in columns(scratch, "email_logs")

    report = asyncio.run(scratch.run(apply_migrations))

    assert "0010_email_logs_suggestion_provenance" in report["applied"], report
    assert {"suggestion_kind", "suggestion_attempt"} <= set(columns(scratch, "email_logs"))

    async def read(conn):
        rows = await conn.execute(text(
            "SELECT suggestion_kind, suggestion_attempt FROM email_logs"
        ))
        return rows.fetchall()

    assert asyncio.run(scratch.run(read)) == [(None, None)], (
        "an old row was given a provenance it never had"
    )

    names = indexes(scratch, "email_logs")
    assert "ix_email_logs_suggestion_kind" in names, (
        "without the index, 'reply rate by kind' is a scan of the whole table"
    )
