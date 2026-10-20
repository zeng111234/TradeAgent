"""Ask a real provider three shape questions, and write down the answers.

This is the scheduled counterpart to the offline suite. The suite proves the
code matches the shape we *believe*; this proves the belief still matches the
provider. It is a script rather than a test because it needs a credential and
the network, and a test that fails without those is a test people learn to ignore.

    OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com \
      OPENAI_MODEL=deepseek-chat python scripts/contract_canary.py

What it writes:
    artifacts/contract_canary/history.jsonl   one line per run, append-only
    artifacts/contract_canary/last.json       the latest record, for a dashboard

The credential is read from the environment and never written to either file.
The record names the base URL and the model, because "the contract held" is only
meaningful for a specific pair.

Exit codes:
    0  shape held
    1  no credential configured (a skip, not a failure)
    2  shape drift -- something the offline suite cannot see
    3  transport failure -- the provider could not be asked

The distinction between 2 and 3 matters: 2 is "fix the parser or pin the
provider", 3 is "try again later". Collapsing them into a red job would teach
the reader to ignore both.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND = SCRIPT_DIR.parent / "backend"
sys.path.insert(0, str(BACKEND))

DEFAULT_OUT = SCRIPT_DIR.parent / "artifacts" / "contract_canary"


def _redact(text: str) -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if key and len(key) > 8:
        return text.replace(key, "sk-***REDACTED***")
    return text


async def _run(client, checks: dict[str, list[str]]) -> None:
    from app.agent.protocol import Message
    from app.canary import (
        TEXT_PROMPT,
        TOOL_PROMPT,
        TOOL_SPEC,
        check_text_reply,
        check_tool_reply,
    )

    text_reply = await asyncio.wait_for(
        client.chat([Message.user(TEXT_PROMPT)], [], max_tokens=32), timeout=60.0
    )
    checks["text"] = check_text_reply(text_reply)

    tool_reply = await asyncio.wait_for(
        client.chat([Message.user(TOOL_PROMPT)], [TOOL_SPEC], max_tokens=128),
        timeout=60.0,
    )
    checks["tool_call"] = check_tool_reply(tool_reply)


def _write(record: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with (out_dir / "history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    (out_dir / "last.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="directory for history.jsonl and last.json")
    args = parser.parse_args()

    from app.agent.protocol import OpenAICompatibleClient
    from app.canary import OK, TRANSPORT_ERROR, build_record
    from app.config import settings
    from app.llm import LLMError, is_configured

    api_key = (settings.OPENAI_API_KEY or "").strip()
    base_url = settings.OPENAI_BASE_URL
    model = settings.OPENAI_MODEL
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not is_configured():
        record = build_record(
            base_url=base_url, model=model, checks={}, checked_at=checked_at,
            status="skipped",
        )
        record["reason"] = "OPENAI_API_KEY is not set; the provider was not asked"
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 1

    client = OpenAICompatibleClient(api_key=api_key, base_url=base_url, model=model)
    checks: dict[str, list[str]] = {}

    try:
        asyncio.run(_run(client, checks))
    except LLMError as exc:
        record = build_record(
            base_url=base_url, model=model, checks=checks, checked_at=checked_at,
            status=TRANSPORT_ERROR,
        )
        record["error"] = _redact(f"{type(exc).__name__}: {exc}")
        _write(record, Path(args.out))
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 3
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        record = build_record(
            base_url=base_url, model=model, checks=checks, checked_at=checked_at,
            status=TRANSPORT_ERROR,
        )
        record["error"] = _redact(f"{type(exc).__name__}: {exc}")
        _write(record, Path(args.out))
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 3

    record = build_record(
        base_url=base_url, model=model, checks=checks, checked_at=checked_at
    )
    _write(record, Path(args.out))
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record["status"] == OK else 2


if __name__ == "__main__":
    raise SystemExit(main())
