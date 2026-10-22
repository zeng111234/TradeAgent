"""Command line behaviour.

The CLI is how a person actually runs this, so its refusal paths matter more
than its success path: it must never fake a result when no model is configured.
"""
from __future__ import annotations

import json

import pytest

from app.cli import build_parser, main


def test_parser_requires_a_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_run_without_a_model_refuses_and_exits_nonzero(capsys, monkeypatch):
    """No fallback. Exit 1 with an actionable message, not a fabricated run."""
    import app.cli as cli

    monkeypatch.setattr(cli, "is_configured", lambda: False, raising=False)
    # is_configured is imported inside the handler, so patch it at the source.
    import app.agent.service as service

    monkeypatch.setattr(service, "is_configured", lambda: False)

    code = main(["run", "review the pipeline"])
    captured = capsys.readouterr()

    assert code == 1
    assert "OPENAI_API_KEY" in captured.err
    assert "not an agent" in captured.err
    assert "demo" in captured.err       # tells the user how to see it work


def test_tools_command_lists_the_registry_and_the_boundary(capsys):
    assert main(["tools"]) == 0
    out = capsys.readouterr().out

    for name in ("list_customers", "get_customer_signals", "search_prospects",
                 "classify_prospect", "create_email_draft", "upsert_customer"):
        assert name in out, f"{name} missing from the tool listing"

    # Required parameters must be marked, or the listing is misleading.
    assert "* customer_id" in out
    assert "no send, delete or dispatch tool exists" in out
    assert "create_email_draft" in out


def test_tools_command_shows_no_sending_tool(capsys):
    main(["tools"])
    out = capsys.readouterr().out
    # Tool names are printed at two spaces with no colon; parameters hang below
    # them at six with a "name: type" shape. Taking every indented line with a
    # colon collected parameters too, so a parameter legitimately called
    # `sender_company` was reported as "a send tool is exposed".
    names = [
        line.strip()
        for line in out.splitlines()
        if line.startswith("  ") and not line.startswith("      ") and ":" not in line
    ]
    assert names, "the listing parsed to nothing -- the format changed, not the boundary"
    for name in names:
        for forbidden in ("send", "dispatch", "delete", "remove"):
            assert forbidden not in name, f"a {forbidden} tool is exposed: {name}"


def test_runs_command_handles_an_empty_history(capsys, monkeypatch, tmp_path):
    """Point the CLI at an empty but valid database.

    The session test database accumulates runs from other tests, so "empty"
    has to be arranged rather than assumed. The schema is created so this
    exercises the "no rows yet" path rather than a missing-table error.
    """
    import asyncio
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import app.database as database

    fresh = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'empty.db').as_posix()}")

    async def prepare():
        async with fresh.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)

    @asynccontextmanager
    async def fresh_session():
        async with async_sessionmaker(fresh, class_=AsyncSession,
                                      expire_on_commit=False)() as session:
            yield session

    asyncio.run(prepare())
    monkeypatch.setattr(database, "async_session", fresh_session)
    # init_db would otherwise (re)create nothing on the real engine and the
    # attribute lookup still resolves to the patched session factory.
    monkeypatch.setattr(database, "engine", fresh)
    try:
        assert main(["runs"]) == 0
        assert "no agent runs recorded yet" in capsys.readouterr().out
    finally:
        asyncio.run(fresh.dispose())


def test_show_unknown_run_exits_nonzero(capsys):
    code = main(["show", "no-such-run-id"])
    assert code == 1
    assert "no run with id" in capsys.readouterr().err


def test_runs_and_show_round_trip(capsys):
    """Seeds a run, then drives the CLI synchronously.

    The CLI calls ``asyncio.run`` internally, so this test must not itself be
    async — nesting would raise "cannot be called from a running event loop".
    """
    import asyncio

    from app.agent.loop import AgentBudget, run_agent
    from app.agent.repository import save_run
    from app.agent.service import build_registry
    from app.database import async_session, init_db
    from tests.scripted_llm import ScriptedLLM, call, final

    async def seed():
        await init_db()
        run = await run_agent(
            "list the quoting customers",
            ScriptedLLM([
                call("list_customers", {"stage": "quoting"}),
                final("One customer needs a nudge before the quote goes stale."),
            ]),
            build_registry(),
            budget=AgentBudget(max_rounds=4),
        )
        async with async_session() as db:
            await save_run(db, run)
        return run

    run = asyncio.run(seed())

    assert main(["runs", "--limit", "5"]) == 0
    listing = capsys.readouterr().out
    assert run.run_id[:8] in listing
    assert "succeeded" in listing

    assert main(["show", run.run_id, "--mermaid"]) == 0
    detail = capsys.readouterr().out
    assert "list_customers" in detail
    assert "stage" in detail
    assert "sequenceDiagram" in detail        # --mermaid was honoured


def test_demo_command_points_at_the_real_script(capsys):
    """`demo` delegates rather than duplicating the demo, so there is one source."""
    import app.cli as cli

    assert cli._cmd_demo.__doc__
    parser = build_parser()
    args = parser.parse_args(["demo"])
    assert args.func is cli._cmd_demo


def test_run_parser_accepts_budget_overrides():
    args = build_parser().parse_args(
        ["run", "goal", "--max-rounds", "3", "--max-tool-calls", "5", "--max-seconds", "30"]
    )
    assert args.max_rounds == 3
    assert args.max_tool_calls == 5
    assert args.max_seconds == 30.0
    assert args.goal == "goal"


def test_run_parser_defaults_to_no_overrides():
    args = build_parser().parse_args(["run", "goal"])
    assert (args.max_rounds, args.max_tool_calls, args.max_seconds) == (0, 0, 0.0)
    assert args.context == ""
    assert args.json is False
    assert args.html is None
