"""Characterisation test for init_db(): it must create every table.

This exists because the previous implementation only created the tables whose
model modules happened to have been imported already. Called from a fresh
process it would create an empty database and fail later with "no such table",
which is a confusing place to discover the problem.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect

from app.database import Base, engine, init_db

EXPECTED_TABLES = {
    "agent_runs",
    "customer_contacts",
    "customer_notes",
    "customers",
    "email_logs",
    "email_templates",
    "tasks",
}


def _sync_engine():
    """A sync engine for the same file.

    The URL is relative (``sqlite+aiosqlite:///./tradeagent.db``), and
    ``sqlite3.connect`` would resolve that against the process CWD rather than
    the engine's root, so inspection goes through SQLAlchemy too.
    """
    return create_engine(str(engine.url).replace("+aiosqlite", ""))


def _table_names() -> set[str]:
    sync = _sync_engine()
    try:
        return set(inspect(sync).get_table_names())
    finally:
        sync.dispose()


async def test_init_db_creates_every_table():
    await init_db()
    missing = EXPECTED_TABLES - _table_names()
    assert not missing, f"init_db() did not create: {sorted(missing)}"


async def test_init_db_registers_models_even_in_a_fresh_metadata():
    """The metadata must be populated by init_db itself, not by side effects."""
    from app.database import Base as Fresh

    await init_db()
    assert "agent_runs" in Fresh.metadata.tables


async def test_init_db_is_idempotent():
    await init_db()
    await init_db()   # must not raise on an existing schema
    assert len(_table_names()) >= len(EXPECTED_TABLES)


def test_agent_run_model_is_importable_from_the_models_package():
    from app.models import AgentRun

    assert AgentRun.__tablename__ == "agent_runs"


def test_every_model_is_registered_on_base_metadata():
    """A model defined but never imported would not be created by init_db."""
    import app.models  # noqa: F401

    assert EXPECTED_TABLES <= set(Base.metadata.tables)
