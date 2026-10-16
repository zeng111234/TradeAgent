"""Prove the migrated legacy path works against a real provider.

The seam change (app/llm.py) rewrote every chat call in the service layer. The
unit tests stub the client, so they prove the wiring, not that DeepSeek accepts
it. This runs the two email paths against the real endpoint and prints the
provenance fields, which is the part a human would read.

    set OPENAI_API_KEY=...  OPENAI_BASE_URL=https://api.deepseek.com
    set OPENAI_MODEL=deepseek-chat
    python scripts/smoke_legacy_llm.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app import llm  # noqa: E402
from app.services.ai_service import generate_cold_email  # noqa: E402
from app.workflows import _generate_lead_draft_email  # noqa: E402

LEAD = {
    "company_name": "EuroTextil GmbH",
    "country": "Germany",
    "website": "https://www.eurotextil.de",
    "snippet": "wholesale embroidery supplies and metallic yarn",
    "emails": ["hans@eurotextil.de"],
}


def show(label: str, result: dict) -> None:
    print(f"--- {label}")
    print(f"    ai_powered      : {result.get('ai_powered')}")
    print(f"    fallback_reason : {result.get('fallback_reason')}")
    print(f"    subject         : {(result.get('subject') or '')[:70]}")
    print(f"    body chars      : {len(result.get('body') or '')}")


async def main() -> int:
    print(f"configured        : {llm.is_configured()}")
    print(f"timeout (seconds) : {llm.timeout_seconds()}")
    print(f"model             : {os.environ.get('OPENAI_MODEL', '(default)')}")
    if not llm.is_configured():
        print("VERDICT: SKIP — no OPENAI_API_KEY in the environment")
        return 0

    started = time.monotonic()
    cold = await generate_cold_email(
        product_name="gold metallic yarn",
        target_industry="Textile & Apparel",
        target_country="Germany",
        company_name="Ningbo Sunshine Textile",
        selling_points="consistent color fastness, low MOQ, 15-day lead time",
    )
    show("generate_cold_email", cold)

    draft = await _generate_lead_draft_email(LEAD, "gold thread, metallic yarn")
    show("_generate_lead_draft_email", draft or {})

    elapsed = time.monotonic() - started
    print(f"elapsed           : {elapsed:.1f}s")

    both_real = (
        cold.get("ai_powered") is True
        and (draft or {}).get("ai_powered") is True
    )
    print(f"VERDICT: {'PASS' if both_real else 'FAIL'} — "
          f"both paths returned model-written text without a fallback")
    return 0 if both_real else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
