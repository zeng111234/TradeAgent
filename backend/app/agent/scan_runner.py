"""Daily prospect scan, run by the agent.

Replaces the fixed loop in the original ``scripts/daily_scan.py``, which was::

    for keyword in keywords:
        for country in countries:
            search()          # 6 hardcoded query templates
            scrape()          # score = 20; +30; +20; +20; +10; drop if < 40
            collect()
    top_leads = sort_by_score()[:10]
    for lead in top_leads: generate_draft_email(lead)   # one LLM call each

Every decision there was made before the model was consulted, and the model
was only ever asked to write copy for candidates that had already survived.

Here the goal goes to the agent, which decides what to search for, which
candidates to open, which to reject and why, and calls ``save_prospects`` when
it is done. Python keeps only what is genuinely deterministic: the memory of
which domains have been seen, the cap on how many prospects one run may add,
and writing the files.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .loop import AgentBudget, AgentRun, run_agent
from .protocol import LLMClient
from .scan_tools import ProspectSink, build_scan_registry
from .service import SCHEDULED_BUDGET, build_llm_client, spill_root

logger = logging.getLogger(__name__)

SCAN_SYSTEM_PROMPT = """You are a foreign-trade prospecting agent for a Chinese \
supplier of embroidery thread, metallic yarn and textile trims, based in Ningbo.

Your job for this run: find genuinely plausible new buyers in the given markets \
and record the ones worth a human's attention.

How to work:
1. Call `already_scanned` first so you do not waste effort on sites already seen.
2. Search with `search_prospects`. Use a few different phrasings — a company that \
imports may describe itself as an importer, a wholesaler, a distributor or a \
sourcing agent. Do not stop at the first query.
3. A search snippet is not evidence. Open the candidates that look plausible with \
`inspect_website` and read what they actually do.
4. Decide. Reject freely and say why. A directory page, a marketplace listing, a \
competitor manufacturer of the same goods, and a site with no way to make contact \
are all rejects. You are looking for companies that buy or import goods like ours.
5. When you have finished judging, call `save_prospects` once, with only the \
candidates you actually inspected and decided to keep.

Rules:
- Never invent an email address, a contact name, or a fact about a company. If \
`inspect_website` found no email, the prospect has no email — record it that way.
- A `score` you record is your own judgement. Do not compute it from a formula, \
and do not reuse a number you saw in a snippet.
- If a site could not be fetched, that is a rejection for this run, not a reason \
to guess what it contains.
- If nothing in the market clears your bar today, save nothing and say so. An \
empty result with a clear reason is a correct result. Do not pad the list to \
look productive.
- Nothing is emailed to anyone. Any `draft_email` you supply is text stored for a \
human to review.

Finish by calling `save_prospects`, then reply with a short plain-text summary: \
how many you kept, how many you rejected, and the single most useful thing you \
learned about the market."""


@dataclass
class ScanOutcome:
    """Everything one scan produced."""

    run: AgentRun
    sink: ProspectSink
    report: str
    leads_path: Optional[Path] = None
    scanned_path: Optional[Path] = None

    @property
    def prospect_count(self) -> int:
        return self.sink.count

    @property
    def kept(self) -> list[dict]:
        return self.sink.prospects

    def as_dict(self) -> dict[str, Any]:
        payload = self.run.as_dict()
        payload["prospects_kept"] = self.prospect_count
        payload["report"] = self.report
        payload["leads_path"] = str(self.leads_path) if self.leads_path else None
        return payload


def build_scan_goal(keywords: list[str], countries: list[str], *, headcount: int = 5) -> str:
    """The goal handed to the agent. Facts and limits, not a procedure."""
    product = ", ".join(keywords) if keywords else "embroidery thread, metallic yarn"
    markets = ", ".join(countries) if countries else "any market"
    return (
        f"Find up to {headcount} new potential buyers for our products "
        f"({product}) in these markets: {markets}.\n\n"
        "We sell these products; we are looking for companies that buy, import or "
        "distribute them rather than make them. For each one you keep, record the "
        "company, its website, the country, any contact email you actually found on "
        "their site, a score out of 100 reflecting your own judgement of fit, your "
        "recommendation, your reasoning, and the evidence from their site that "
        "supports it.\n\n"
        "Quality matters more than count. If only two candidates are convincing, keep "
        "two and say why the rest were rejected."
    )


async def run_scan(
    *,
    keywords: list[str],
    countries: list[str],
    out_dir: Path,
    scanned_path: Optional[Path] = None,
    headcount: int = 5,
    llm: Optional[LLMClient] = None,
    budget: Optional[AgentBudget] = None,
    commit: bool = True,
) -> ScanOutcome:
    """Run one daily scan.

    ``commit=False`` (dry run) leaves both output files untouched, so a bad
    model day cannot overwrite a good previous result.
    """
    out_dir = Path(out_dir)
    scanned_path = Path(scanned_path) if scanned_path else out_dir / "scanned_domains.json"

    sink = ProspectSink(
        out_dir=out_dir,
        scanned_path=scanned_path,
        max_prospects=max(headcount, 1),
    )
    previously_seen = sink.load_scanned()

    registry = build_scan_registry(sink, allow_save=commit)
    client = llm or build_llm_client()

    goal = build_scan_goal(keywords, countries, headcount=headcount)
    context = (
        f"Already-scanned domain memory contains {len(previously_seen)} domain(s). "
        f"Call the `already_scanned` tool if you need the list."
    )

    run = await run_agent(
        goal,
        client,
        registry,
        budget=budget or SCHEDULED_BUDGET,
        context=context,
        system_prompt=SCAN_SYSTEM_PROMPT,
        spill_dir=spill_root(),
    )

    report = sink.build_report(
        keywords=keywords, countries=countries,
        skipped=len(previously_seen), run=run,
    )

    leads_path = None
    if commit:
        leads_path = sink.write_leads(keywords=keywords, countries=countries)
        sink.save_scanned()
        scanned_path = sink.scanned_path
    else:
        logger.info("dry run: leaving %s and %s untouched", out_dir / "daily_leads.json", scanned_path)

    await _persist(run)

    return ScanOutcome(
        run=run,
        sink=sink,
        report=report,
        leads_path=leads_path,
        scanned_path=sink.scanned_path if commit else None,
    )


async def find_leads_agentic(
    *,
    product_keywords: str,
    target_country: str = "",
    target_region: str = "",
    max_results: int = 10,
    llm: Optional[LLMClient] = None,
    budget: Optional[AgentBudget] = None,
    work_dir: Optional[Path] = None,
) -> dict:
    """Agent-driven replacement for ``agent_service.auto_lead_scanner``.

    Keeps that function's response shape so the existing
    ``POST /api/v1/agent/scan-leads`` endpoint and its frontend keep working,
    but the leads are now the agent's judgement rather than the output of a
    scoring formula. The old implementation filtered candidates through
    ``relevance_score >= 30`` built from six constants *before* the model was
    consulted, so the model could only ever see candidates the formula already
    approved.

    No files are written: this is an interactive lookup, not the daily job.
    """
    import tempfile

    keywords = [k.strip() for k in (product_keywords or "").split(",") if k.strip()]
    if not keywords:
        raise ValueError("product_keywords must not be empty")

    region = (target_region or "").strip()
    country = (target_country or "").strip()
    market = f"{country} {region}".strip() or "any market"

    tmp = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="tradeagent-scan-"))

    sink = ProspectSink(
        out_dir=tmp,
        scanned_path=tmp / "scanned_domains.json",
        max_prospects=max(1, min(int(max_results or 10), 20)),
        remember_domains=False,     # an interactive lookup must not poison the daily memory
    )
    registry = build_scan_registry(sink, allow_save=True)
    client = llm or build_llm_client()

    goal = (
        f"Find up to {sink.max_prospects} potential buyers for our products "
        f"({', '.join(keywords)}) in {market}.\n\n"
        "We sell these products; we want companies that buy, import or distribute "
        "them. Open the plausible candidates and judge them yourself, then call "
        "`save_prospects` with the ones worth approaching and `report_scan_findings` "
        "with what you learned about the market. Give each kept prospect a score out "
        "of 100 that reflects your own judgement, a recommendation, your reasoning, "
        "the evidence you used, any contact email you actually found on their site, "
        "and a suggested approach angle."
    )

    run = await run_agent(
        goal,
        client,
        registry,
        budget=budget or SCHEDULED_BUDGET,
        context=f"Markets requested: {market}. No prior domain memory applies to this lookup.",
        system_prompt=SCAN_SYSTEM_PROMPT,
        spill_dir=spill_root(),
    )

    await _persist(run)

    leads = [
        {
            "company_name": p["company_name"],
            "website": p["website"],
            "country": p["country"] or country or "Unknown",
            "emails": p["emails"],
            "phones": p["phones"],
            "relevance_score": p["relevance_score"],
            "page_title": p["company_name"],
            "snippet": p["snippet"],
            "has_contact": bool(p["emails"]),
            "ai_priority": _priority_of(p["agent_recommendation"]),
            "ai_reason": p["agent_reasoning"],
            "approach_angle": p["agent_recommendation"],
            "estimated_potential": "unknown",
        }
        for p in sink.prospects
    ]

    return {
        "total_found": len(leads),
        "leads": leads,
        "market_insights": sink.market_insights,
        "search_tips": sink.search_tips or "Refine the product keywords or narrow the market.",
        "ai_powered": True,
        # Kept for shape compatibility. Real query strings are visible in the
        # run trace instead, because the agent chooses them.
        "search_queries_used": [],
        "agent_run": {
            "run_id": run.run_id,
            "status": run.status,
            "stop_reason": run.stop_reason,
            "tool_sequence": run.tool_sequence,
            "rounds": len(run.steps),
            "tool_calls": run.tool_call_count,
        },
    }


_RECOMMENDATION_TO_PRIORITY = {
    "approach_now": "high",
    "approach_later": "medium",
    "low_priority": "low",
    "not_a_fit": "low",
}


def _priority_of(recommendation: str) -> str:
    return _RECOMMENDATION_TO_PRIORITY.get((recommendation or "").strip().lower(), "medium")


async def _persist(run: AgentRun) -> None:
    """Best-effort persistence of the run trace.

    Ensures the schema exists first. Standalone entry points (the CLI, a cron
    job) never run the FastAPI lifespan, so without this the trace would be
    silently lost — and the whole point of the run record is that it survives.
    """
    try:
        from app.database import async_session, init_db

        from .repository import save_run

        await init_db()
        async with async_session() as db:
            await save_run(db, run)
    except Exception as exc:  # noqa: BLE001 - a lost trace must not fail a scan
        logger.warning("could not persist scan run %s: %s", run.run_id, exc)
