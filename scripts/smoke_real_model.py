"""Smoke-test the agent against a real model, end to end.

Every other test in this repository uses a scripted model, which is what makes
them fast and deterministic — and which means none of them prove the agent works
against a real provider. This script does the opposite: it drives the real HTTP
endpoint, the real tool registry, the real loop and a real model.

It is deliberately a separate script rather than a pytest test, because it needs
a credential and the network, and a test suite that fails without those is a test
suite people learn to ignore.

    # Windows PowerShell
    $env:OPENAI_API_KEY  = "sk-..."
    $env:OPENAI_BASE_URL = "https://api.deepseek.com"
    $env:OPENAI_MODEL    = "deepseek-chat"
    python scripts/smoke_real_model.py

    # bash
    OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com \
      OPENAI_MODEL=deepseek-chat python scripts/smoke_real_model.py

The credential is read from the environment and never written anywhere. Nothing
in this script prints it.

Exit codes:
    0  the agent produced a run that used tools and reached a conclusion
    1  no credential configured
    2  the run failed, or answered without investigating
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND = SCRIPT_DIR.parent / "backend"
sys.path.insert(0, str(BACKEND))

# A goal that needs tool use and reaches no external network: it reads the local
# database. That isolates "does the agent call tools correctly against a real
# model" from "does web search work today".
GOAL = (
    "Review my customers and tell me which one most needs attention today. "
    "Explain your reasoning and cite the specific numbers you used."
)

CONTEXT = "We are a Ningbo textile supplier. Be concise; this is a daily check-in."


def redact(text: str) -> str:
    """Remove anything that looks like the credential, in case a provider echoes it."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if key and len(key) > 8:
        return text.replace(key, "sk-***REDACTED***")
    return text


def wait_for_run(client, started: dict, *, timeout: float = 300.0) -> dict:
    """Follow the poll URL until the run leaves ``running``.

    ``POST /agent/run`` answers with a run id unless ``wait=true`` -- and
    ``wait=true`` caps the request at ``WAIT_MODE_MAX_SECONDS`` (100s), below the
    goal this script asks for. Polling is both the only way to see the finished
    trace and the path a real caller uses.
    """
    import time

    poll = started.get("poll") or f"/api/v1/agent/runs/{started['run_id']}"
    body = started
    deadline = time.monotonic() + timeout

    while body.get("status") == "running":
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"run {started.get('run_id')} did not finish within {timeout:.0f}s"
            )
        time.sleep(0.5)
        response = client.get(poll)
        if response.status_code == 200:
            body = response.json()

    return body


def main() -> int:
    from fastapi.testclient import TestClient

    from app.main import app
    from app.agent.service import is_configured

    base_url = os.environ.get("OPENAI_BASE_URL", "")
    model = os.environ.get("OPENAI_MODEL", "")

    print("=" * 68)
    print("TradeAgent — real-model smoke test")
    print("=" * 68)
    print(f"  base url : {base_url or '(default)'}")
    print(f"  model    : {model or '(default)'}")
    print(f"  key      : {'set, ' + str(len(os.environ.get('OPENAI_API_KEY', ''))) + ' chars' if os.environ.get('OPENAI_API_KEY') else 'NOT SET'}")

    if not is_configured():
        print("\n[fatal] No model configured. Set OPENAI_API_KEY at minimum.")
        return 1

    # Entering the lifespan creates the schema and seeds the demo customers, so
    # the agent has something real to read.
    with TestClient(app) as client:
        health = client.get("/health")
        print(f"\n  /health  : {health.status_code} {health.json()}")

        caps = client.get("/api/v1/agent/capabilities").json()
        print(f"  tools    : {caps['tool_count']} offered, configured={caps['configured']}")
        print(f"  boundary : cannot {', '.join(caps['cannot'])}")

        print("\n--- running the agent (real model, may take a minute) ---\n")
        response = client.post(
            "/api/v1/agent/run",
            json={"goal": GOAL, "context": CONTEXT, "max_rounds": 10, "max_seconds": 240},
        )
        if response.status_code not in (200, 202):
            print(redact(f"[fatal] HTTP {response.status_code}: {response.text[:600]}"))
            return 2

        body = response.json()
        if "stop_reason" not in body:
            print(f"  started      : {body['run_id']} (polling until it finishes)")
            body = wait_for_run(client, body)

    # The detail endpoint carries the steps, not the list summary's derived
    # fields. Deriving them here is deliberate: it is the same arithmetic a
    # reader would do, and it keeps this script honest about what the API
    # actually returns rather than asking the API to grow a shape for a script.
    steps = body["steps"]
    calls = [call for step in steps for call in step["tool_calls"]]
    tool_sequence = [call["name"] for call in calls if call.get("ok")]
    refused = sum(1 for call in calls if call.get("refused_reason"))
    did_investigate = bool(tool_sequence)
    elapsed = round((body.get("elapsed_ms") or 0) / 1000, 1)

    print(f"  status       : {body['status']} ({body['stop_reason']})")
    print(f"  tool path    : {' -> '.join(tool_sequence) or '(none)'}")
    print(f"  rounds/calls : {body['rounds']}/{body['tool_calls']}")
    print(f"  refused      : {refused}")
    print(f"  did research : {did_investigate}")
    print(f"  elapsed      : {elapsed}s")
    print(f"  tokens       : {body['prompt_tokens']} in / {body['completion_tokens']} out")
    print(f"  run id       : {body['run_id']}")

    print("\n--- per-round trace ---")
    for step in steps:
        thought = (step.get("thought") or "").strip().replace("\n", " ")
        print(f"\n  round {step['round']}" + (f": {thought[:150]}" if thought else ""))
        for call in step["tool_calls"]:
            flag = "ok " if call["ok"] else ("REF" if call.get("refused_reason") else "ERR")
            args = json.dumps(call["arguments"], ensure_ascii=False)
            print(f"      [{flag}] {call['name']}({args[:110]})")
            observation = (call.get("observation") or "").replace("\n", " ")
            print(f"            -> {observation[:170]}")

    print("\n--- conclusion ---")
    print(body["final_output"] or "(no conclusion produced)")

    # The diagram must be renderable from a real run, not only from a scripted one.
    out = SCRIPT_DIR.parent / "artifacts" / "smoke"
    out.mkdir(parents=True, exist_ok=True)
    (out / "last-run.mmd").write_text(body.get("mermaid_sequence", ""), encoding="utf-8")
    if body.get("mermaid_flowchart"):
        (out / "last-run-flow.mmd").write_text(
            body["mermaid_flowchart"], encoding="utf-8"
        )
    (out / f"{body['run_id']}.json").write_text(
        json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  trace written to {out}")

    # --- verdict ----------------------------------------------------------
    problems = []
    if body["status"] != "succeeded":
        problems.append(f"run ended '{body['status']}' ({body['stop_reason']})")
    if not did_investigate:
        problems.append("the agent answered without calling a single tool")
    if not body["final_output"]:
        problems.append("no conclusion was produced")

    if problems:
        print("\n[FAIL]")
        for problem in problems:
            print(f"  - {problem}")
        return 2

    print("\n[PASS] the agent used tools and reached a conclusion against a real model")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
