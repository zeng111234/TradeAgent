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

    if response.status_code != 200:
        print(redact(f"[fatal] HTTP {response.status_code}: {response.text[:600]}"))
        return 2

    body = response.json()

    print(f"  status       : {body['status']} ({body['stop_reason']})")
    print(f"  tool path    : {' -> '.join(body['tool_sequence']) or '(none)'}")
    print(f"  rounds/calls : {body['rounds']}/{body['tool_calls']}")
    print(f"  refused      : {body.get('refused_calls', 0)}")
    print(f"  did research : {body['did_investigate']}")
    print(f"  elapsed      : {body['elapsed']}s")
    print(f"  tokens       : {body['prompt_tokens']} in / {body['completion_tokens']} out")
    print(f"  run id       : {body['run_id']}")

    print("\n--- per-round trace ---")
    for step in body["steps"]:
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
    (out / "last-run.mmd").write_text(body["mermaid_sequence"], encoding="utf-8")
    (out / "last-run-flow.mmd").write_text(body["mermaid_flowchart"], encoding="utf-8")
    (out / f"{body['run_id']}.json").write_text(
        json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  trace written to {out}")

    # --- verdict ----------------------------------------------------------
    problems = []
    if body["status"] != "succeeded":
        problems.append(f"run ended '{body['status']}' ({body['stop_reason']})")
    if not body["did_investigate"]:
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
