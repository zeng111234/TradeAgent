"""Daily lead scan — agent-driven.

Drop-in replacement for the previous version of this script. Same inputs (all
via environment variables, because that is how GitHub Actions drives it), same
output files, same report email. What changed is who decides which prospects
are worth keeping.

Previously this file contained the decision itself::

    score = 20
    if is_b2b:              score += 30
    if buyer_keyword:       score += 20
    if product_keyword:     score += 20
    if emails_found:        score += 10
    if score >= 40:         keep()

and the model was called once per surviving candidate, purely to write copy.
Now the agent chooses what to search, what to open, what to reject and why; the
hardcoded scorer is gone.

Environment:
    SCAN_KEYWORDS        comma separated product keywords
    SCAN_COUNTRIES       comma separated markets; empty means the global default
    SCAN_HEADCOUNT       how many prospects to aim for (default 5)
    SCAN_DRY_RUN         "1" to run without writing files or sending mail
    SCAN_COMMIT          "0" to skip writing output files
    REPORT_EMAIL         where the report goes
    SMTP_*               SMTP credentials (absent => report printed to stdout)
    OPENAI_API_KEY       required; without it this script exits non-zero
    OPENAI_BASE_URL, OPENAI_MODEL

Exit codes:
    0  scan completed (zero prospects kept is a valid result — when the search worked)
    1  no model configured
    2  the agent run failed outright
    3  every external lookup failed (all searches, or every page fetch with
       nothing kept), so nothing was evaluated -- not a quiet market
"""
from __future__ import annotations

import asyncio
import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
BACKEND_DIR = REPO_DIR / "backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

GLOBAL_COUNTRIES = [
    "Germany", "United States", "United Kingdom", "France", "Italy",
    "Spain", "Australia", "India", "Japan", "South Korea",
    "Brazil", "Turkey", "Netherlands", "Canada", "Mexico",
]
DEFAULT_KEYWORDS = "embroidery thread, gold metallic yarn, metallic thread, gold yarn"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def send_report_email(report: str, prospect_count: int) -> None:
    """Send the report. Absent SMTP config prints instead of failing."""
    host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    recipient = os.environ.get("REPORT_EMAIL") or user

    if not user or not password:
        print("[info] SMTP not configured — printing the report instead")
        print(report)
        return

    message = MIMEMultipart("alternative")
    date = datetime.now().strftime("%Y-%m-%d")
    message["Subject"] = f"TradeAgent daily report — {date} ({prospect_count} new prospects)"
    message["From"] = user
    message["To"] = recipient
    message.attach(MIMEText(report, "plain", "utf-8"))
    message.attach(
        MIMEText(
            '<div style="font-family:monospace;font-size:13px;max-width:760px">'
            + report.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
            + "</div>",
            "html",
            "utf-8",
        )
    )

    try:
        with smtplib.SMTP_SSL(host, port) as server:
            server.login(user, password)
            server.sendmail(user, [recipient], message.as_string())
        print(f"[ok] report emailed to {recipient}")
    except Exception as exc:  # noqa: BLE001
        print(f"[error] could not send report: {exc}")
        print(report)


def _demo_run(keywords: list[str], countries: list[str], headcount: int) -> int:
    """Exercise the whole scan path offline, against a temp directory.

    Uses the real loop, the real tools, the real sink and the real report. Only
    the model and the two network-backed tools are replaced. Exists so the
    pipeline can be demonstrated and smoke-tested without credentials.
    """
    import tempfile

    from app.agent.protocol import LLMReply, ToolCall
    from app.agent.scan_runner import run_scan
    from app.agent.tools import Tool, ToolRegistry

    work = Path(tempfile.mkdtemp(prefix="tradeagent-scan-demo-"))

    class ScriptedLLM:
        """Same protocol as the real client; replays a fixed script."""

        def __init__(self, script):
            self.script = script
            self.index = 0

        async def chat(self, messages, tools, *, temperature=0.2, max_tokens=1500):
            if self.index >= len(self.script):
                return LLMReply(content="(script exhausted)")
            entry = self.script[self.index]
            self.index += 1
            return LLMReply(
                content=entry.get("content", ""),
                tool_calls=[
                    ToolCall(id=f"c{i}", name=tc["name"],
                             arguments=__import__("json").dumps(tc["arguments"]))
                    for i, tc in enumerate(entry.get("tool_calls", []))
                ],
                prompt_tokens=180, completion_tokens=90,
            )

    def _call(name, arguments=None):
        return {"tool_calls": [{"name": name, "arguments": arguments or {}}]}

    canned_search = {
        "count": 2,
        "queries_run": [f"{keywords[0]} wholesaler {countries[0]}", f"{keywords[0]} importer {countries[0]}"],
        "candidates": [
            {"company_name": "Example Textil GmbH", "website": "https://example-textil.example",
             "domain": "example-textil.example", "snippet": "Wholesaler of embroidery threads",
             "search_keyword": keywords[0], "search_country": countries[0]},
            {"company_name": "Textile Buyer Directory", "website": "https://directory.example",
             "domain": "directory.example", "snippet": "Directory of European textile buyers",
             "search_keyword": keywords[0], "search_country": countries[0]},
        ],
        "errors": [],
        "note": "Unranked and unscored by design.",
    }
    canned_page = {
        "url": "https://example-textil.example", "status_code": 200,
        "title": "Example Textil GmbH - Embroidery Threads and Metallic Yarns",
        "emails": ["einkauf@example-textil.example"], "phones": ["+49 40 123456"],
        "text": "Importer and wholesaler of embroidery threads, metallic yarns and trims "
                "for the German apparel industry since 1978.",
        "text_truncated": False, "text_total_chars": 130,
    }

    script = [
        _call("already_scanned"),
        _call("search_prospects", {"keyword": keywords[0], "country": countries[0]}),
        _call("inspect_website", {"url": "https://example-textil.example"}),
        _call("save_prospects", {"prospects": [{
            "company_name": "Example Textil GmbH",
            "website": "https://example-textil.example",
            "country": countries[0],
            "industry": "Textile",
            "keyword": keywords[0],
            "why": "Imports embroidery threads for the German apparel market",
            "emails": ["einkauf@example-textil.example"],
            "score": 82,
            "recommendation": "approach_now",
            "reasoning": "Their site says they import and wholesale embroidery threads, "
                         "which is precisely our product line, and they publish a purchasing address.",
            "evidence": "Importer and wholesaler of embroidery threads and metallic yarns since 1978.",
        }]}),
        _call("report_scan_findings", {
            "market_insights": "German textile buyers describe themselves as wholesalers rather "
                               "than importers, so 'wholesaler' is the more productive query.",
            "search_tips": "'importer' returned mostly marketplace directories; 'wholesaler' "
                           "returned companies that actually stock the product.",
        }),
        {"content": "Kept one prospect. Rejected the directory site, which stocks nothing and "
                    "offers no route to a purchasing contact, so approaching it would waste a turn."},
    ]

    # Rebuild the registry with the two network tools replaced.
    import app.agent.scan_runner as runner
    import app.agent.scan_tools as scan_tools

    real_build = scan_tools.build_scan_registry

    def patched_build(sink, *, allow_save=True):
        base = real_build(sink, allow_save=allow_save)
        swapped = ToolRegistry()

        async def stub_search(keyword: str, country: str = "", max_results: int = 8) -> dict:
            return canned_search

        async def stub_inspect(url: str, max_chars: int = 3000) -> dict:
            page = dict(canned_page)
            page["url"] = url
            return page

        stubs = {"search_prospects": stub_search, "inspect_website": stub_inspect}
        for name in base.names():
            original = base.get(name)
            swapped.register(
                Tool(name, original.description, original.params_model,
                     stubs.get(name, original.fn), set(original.tags))
            )
        return swapped

    runner.build_scan_registry = patched_build
    try:
        outcome = asyncio.run(run_scan(
            keywords=keywords, countries=countries, out_dir=work,
            headcount=headcount, llm=ScriptedLLM(script), commit=True,
        ))
    finally:
        runner.build_scan_registry = real_build

    print(outcome.report)
    print()
    print(f"[demo] artefacts     : {work}")
    print(f"[demo] tool path     : {' -> '.join(outcome.run.tool_sequence)}")
    print(f"[demo] prospects kept: {outcome.prospect_count}")
    print("[demo] offline — no model was called and no network request was made")
    return 0


def main(argv: list[str] | None = None) -> int:
    from app.agent.scan_runner import run_scan
    from app.agent.service import is_configured

    argv = list(sys.argv[1:] if argv is None else argv)
    demo = "--demo" in argv

    keywords = [
        k.strip()
        for k in os.environ.get("SCAN_KEYWORDS", DEFAULT_KEYWORDS).split(",")
        if k.strip()
    ]
    countries_raw = os.environ.get("SCAN_COUNTRIES", "").strip()
    countries = (
        [c.strip() for c in countries_raw.split(",") if c.strip()]
        if countries_raw
        else GLOBAL_COUNTRIES
    )
    headcount = int(os.environ.get("SCAN_HEADCOUNT", "5") or 5)

    dry_run = _env_flag("SCAN_DRY_RUN")
    commit = _env_flag("SCAN_COMMIT", default=True) and not dry_run

    print("=" * 62)
    print(f"TradeAgent daily scan — {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  keywords : {', '.join(keywords)}")
    print(f"  markets  : {', '.join(countries[:6])}{'...' if len(countries) > 6 else ''}")
    print(f"  target   : {headcount} prospect(s)")
    print(f"  mode     : {'demo (offline)' if demo else ('dry run' if dry_run else ('commit' if commit else 'read-only'))}")
    print("=" * 62)

    if demo:
        return _demo_run(keywords, countries, headcount)

    if not is_configured():
        print(
            "[fatal] No LLM configured. Set OPENAI_API_KEY (and optionally "
            "OPENAI_BASE_URL / OPENAI_MODEL).\n"
            "        There is no template fallback: a scan that cannot judge is not "
            "a scan, and pretending otherwise is the failure this rewrite removes.\n"
            "        Run `python scripts/daily_scan.py --demo` to exercise the "
            "pipeline offline instead."
        )
        return 1

    outcome = asyncio.run(
        run_scan(
            keywords=keywords,
            countries=countries,
            out_dir=SCRIPT_DIR,
            headcount=headcount,
            commit=commit,
        )
    )

    run = outcome.run
    print()
    print(f"[run] status      : {run.status} ({run.stop_reason})")
    print(f"[run] tool path   : {' -> '.join(run.tool_sequence) or '(none)'}")
    print(f"[run] rounds/calls: {len(run.steps)}/{run.tool_call_count}")
    print(f"[run] tokens      : {run.prompt_tokens} in / {run.completion_tokens} out")
    print(f"[run] kept        : {outcome.prospect_count} prospect(s)")
    if outcome.search_errors:
        print(
            f"[run] search errs : {len(outcome.search_errors)} "
            f"({'ALL queries failed -- not a quiet market' if outcome.search_failed_entirely else 'partial, run continued'})"
        )
    if outcome.inspect_errors:
        print(
            f"[run] fetch errs  : {len(outcome.inspect_errors)} "
            f"({'ALL page fetches failed' if outcome.inspection_failed_entirely else 'partial, run continued'})"
        )
    if outcome.leads_path:
        print(f"[out] leads       : {outcome.leads_path}")
        print(f"[out] domains     : {outcome.scanned_path}")

    # Machine-readable summary for CI logs and for the workflow to diff.
    print("\n--- summary json ---")
    print(
        json.dumps(
            {
                "run_id": run.run_id,
                "status": run.status,
                "stop_reason": run.stop_reason,
                "tool_sequence": run.tool_sequence,
                "rounds": len(run.steps),
                "tool_calls": run.tool_call_count,
                "prospects_kept": outcome.prospect_count,
                # A zero here is only "quiet market" when the search worked.
                "search_errors": len(outcome.search_errors),
                "search_failed_entirely": outcome.search_failed_entirely,
                "inspect_errors": len(outcome.inspect_errors),
                "inspection_failed_entirely": outcome.inspection_failed_entirely,
                "dry_run": dry_run,
            },
            ensure_ascii=False,
        )
    )

    if dry_run:
        print("\n(dry run: no files written, no email sent)")
        return 0

    send_report_email(outcome.report, outcome.prospect_count)
    if run.status == "error":
        return 2
    if outcome.search_failed_entirely:
        # The report says so too; the exit code is what turns a silent zero
        # into a red workflow someone can see.
        print("[fatal] every search query failed: nothing was evaluated today")
        return 3
    if outcome.inspection_failed_entirely and outcome.prospect_count == 0:
        print("[fatal] every page fetch failed: no candidate could be evaluated")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
