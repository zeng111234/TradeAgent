"""TradeAgent command line.

    python -m app.cli run "看看我现有客户里哪些需要今天跟进，给我理由"
    python -m app.cli run "..." --context "我们下周参加法兰克福展会" --html out/
    python -m app.cli tools
    python -m app.cli runs --limit 10
    python -m app.cli show <run_id>
    python -m app.cli demo

Run from ``backend/``, or with ``PYTHONPATH`` pointing at it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _cmd_run(args: argparse.Namespace) -> int:
    from app.agent.loop import AgentBudget
    from app.agent.render import render_html
    from app.agent.service import (
        DEFAULT_BUDGET, AgentUnavailable, execute, is_configured, summarise,
    )

    if not is_configured():
        print(
            "No LLM configured. Set OPENAI_API_KEY (and optionally OPENAI_BASE_URL / "
            "OPENAI_MODEL) in the environment or backend/.env.\n"
            "There is no fallback: an agent that cannot decide is not an agent.\n"
            "To see the runtime work without credentials, run `tradeagent demo`.",
            file=sys.stderr,
        )
        return 1

    budget = AgentBudget(
        max_rounds=args.max_rounds,
        max_tool_calls=args.max_tool_calls,
        max_seconds=args.max_seconds,
    ) if any((args.max_rounds, args.max_tool_calls, args.max_seconds)) else DEFAULT_BUDGET

    async def go():
        return await execute(args.goal, context=args.context, budget=budget)

    try:
        result = asyncio.run(go())
    except AgentUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1

    summary = summarise(result)
    print()
    print(f"status      : {summary['status']} ({summary['stop_reason']})")
    print(f"tool path   : {' -> '.join(summary['tool_sequence']) or '(none)'}")
    print(f"rounds/calls: {summary['rounds']}/{summary['tool_calls']}")
    print(f"elapsed     : {summary['elapsed']}s")
    print(f"run id      : {summary['run_id']}")
    if result.run.error:
        print(f"error       : {result.run.error}", file=sys.stderr)

    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))

    if args.html:
        outdir = Path(args.html)
        outdir.mkdir(parents=True, exist_ok=True)
        page = outdir / f"{result.run.run_id}.html"
        page.write_text(render_html(result.run), encoding="utf-8")
        diagram = outdir / f"{result.run.run_id}.mmd"
        diagram.write_text(result.mermaid_sequence, encoding="utf-8")
        print(f"html        : {page}")
        print(f"mermaid     : {diagram}")

    if result.run.final_output:
        print()
        print(result.run.final_output)

    return 2 if result.run.status == "error" else 0


def _cmd_tools(_args: argparse.Namespace) -> int:
    """List the tools the agent is offered, including the per-run ones.

    `spawn_researcher` and `recall_observation` are attached when a run starts
    rather than existing in the base registry, so listing only the base would
    understate what the agent can do.
    """
    from app.agent.loop import AgentBudget, AgentLoop
    from app.agent.service import build_registry, spill_root

    class _NoModel:
        """Registration needs a client, not a working one."""

        async def chat(self, *args, **kwargs):  # pragma: no cover - never called
            raise RuntimeError("the tool listing does not call a model")

    registry = build_registry()
    from app.agent.subagent import attach_delegation

    record = attach_delegation(registry, llm=_NoModel())

    # AgentLoop registers recall_observation when a spill store is available;
    # constructing one is how the listing stays truthful.
    AgentLoop(_NoModel(), registry, budget=AgentBudget(), spill_dir=spill_root())

    print(f"{len(registry)} tools offered to the agent\n")
    for entry in registry.schemas():
        fn = entry["function"]
        params = fn["parameters"]
        required = set(params.get("required", []))
        print(f"  {fn['name']}")
        print(f"      {fn['description']}")
        for name, spec in params["properties"].items():
            mark = "*" if name in required else " "
            print(f"      {mark} {name}: {spec.get('type', 'any')}")
        print()

    writers = [t.name for t in registry.tagged("write")]
    print(f"write-capable tools: {', '.join(writers) or '(none)'}")
    print("no send, delete or dispatch tool exists — enforced by omission")
    if record is not None and "spawn_researcher" in registry:
        print("sub-agents: depth 1, read-only, cannot delegate further")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    from app.agent.repository import list_runs
    from app.database import async_session, init_db

    async def go():
        await init_db()
        async with async_session() as db:
            return await list_runs(db, limit=args.limit, status=args.status)

    rows = asyncio.run(go())
    if not rows:
        print("no agent runs recorded yet")
        return 0

    print(f"{len(rows)} run(s), newest first\n")
    print(f"  {'id':<10} {'status':<10} {'calls':>5} {'rounds':>6}  goal")
    for row in rows:
        print(f"  {row.run_id[:8]:<10} {row.status:<10} {row.tool_calls:>5} {row.rounds:>6}  "
              f"{(row.goal or '')[:60]}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    from app.agent.render import render_mermaid_sequence
    from app.agent.repository import get_run, trace_of
    from app.database import async_session, init_db

    async def go():
        await init_db()
        async with async_session() as db:
            return await get_run(db, args.run_id)

    row = asyncio.run(go())
    if row is None:
        print(f"no run with id {args.run_id}", file=sys.stderr)
        return 1

    trace = trace_of(row)
    print(f"run      : {row.run_id}")
    print(f"goal     : {row.goal}")
    print(f"status   : {row.status} ({row.stop_reason})")
    print(f"rounds   : {row.rounds}   tool calls: {row.tool_calls}")
    print(
        f"tokens   : {row.prompt_tokens} in "
        f"({getattr(row, 'cached_prompt_tokens', 0) or 0} cached) / "
        f"{row.completion_tokens} out"
    )
    print()
    for step in trace:
        thought = (step.get("thought") or "").strip()
        print(f"round {step.get('round')}" + (f": {thought[:100]}" if thought else ""))
        for call in step.get("tool_calls", []):
            flag = "ok " if call.get("ok") else "ERR"
            print(f"    [{flag}] {call.get('name')}({json.dumps(call.get('arguments'), ensure_ascii=False)})")
            observation = (call.get("observation") or "").replace("\n", " ")
            print(f"          -> {observation[:150]}")
    if row.final_output:
        print()
        print("conclusion:")
        print(row.final_output)
    if args.mermaid:
        print()
        print(render_mermaid_sequence({
            "goal": row.goal, "status": row.status,
            "stop_reason": row.stop_reason, "steps": trace,
        }))
    return 0


def _cmd_demo(_args: argparse.Namespace) -> int:
    """Offline demonstration; delegates so there is a single implementation."""
    import subprocess

    script = Path(__file__).resolve().parent.parent.parent / "scripts" / "demo_agent_run.py"
    if not script.exists():
        print(f"demo script not found at {script}", file=sys.stderr)
        return 1
    return subprocess.call([sys.executable, str(script)])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tradeagent",
        description="Run the TradeAgent agent, inspect its traces, list its tools.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the agent against a goal")
    run.add_argument("goal", help="what you want the agent to achieve")
    run.add_argument("--context", default="", help="extra background for this run")
    run.add_argument("--html", metavar="DIR", help="write an HTML trace page and Mermaid source")
    run.add_argument("--json", action="store_true", help="print the full trace as JSON")
    run.add_argument("--max-rounds", type=int, default=0)
    run.add_argument("--max-tool-calls", type=int, default=0)
    run.add_argument("--max-seconds", type=float, default=0.0)
    run.set_defaults(func=_cmd_run)

    tools = sub.add_parser("tools", help="list the tools the agent may call")
    tools.set_defaults(func=_cmd_tools)

    runs = sub.add_parser("runs", help="list recorded runs")
    runs.add_argument("--limit", type=int, default=20)
    runs.add_argument("--status", default=None,
                      help="filter by status: succeeded|truncated|stalled|error")
    runs.set_defaults(func=_cmd_runs)

    show = sub.add_parser("show", help="show one recorded run")
    show.add_argument("run_id")
    show.add_argument("--mermaid", action="store_true", help="also print the Mermaid diagram")
    show.set_defaults(func=_cmd_show)

    demo = sub.add_parser("demo", help="offline demonstration of two different goals")
    demo.set_defaults(func=_cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
