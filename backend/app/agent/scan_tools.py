"""Tools for the daily prospect scan.

These are the two pieces of the old ``scripts/daily_scan.py`` that are *not*
judgement, and therefore belong in Python:

* recalling which domains have already been seen, and
* writing the result to the file the rest of the pipeline already reads.

What does *not* belong in Python — and is therefore absent here — is deciding
which candidates are worth keeping. The previous implementation did that with::

    score = 20
    if is_b2b_platform:       score += 30
    if buyer_keyword_in_text: score += 20
    if product_keyword_match: score += 20
    if emails_found:          score += 10
    if score >= 40: keep()

Those constants silently discarded most of what the search returned. Here the
agent reads each candidate's site and decides, and every decision lands in the
trace with its reasoning.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .tools import ToolRegistry, tool

logger = logging.getLogger(__name__)

# Domains are re-scanned after this long, so a site that added a contact page
# since last time gets another look.
SCAN_EXPIRE_DAYS = 7
MAX_PROSPECTS = 12


class ProspectSink:
    """Collects evaluated prospects and writes the artefacts the pipeline reads.

    Mutable state lives here rather than in module globals so two concurrent
    runs cannot corrupt each other's output.
    """

    def __init__(
        self,
        out_dir: Path,
        scanned_path: Path,
        *,
        max_prospects: int = MAX_PROSPECTS,
        remember_domains: bool = True,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.scanned_path = Path(scanned_path)
        self.max_prospects = max(1, min(int(max_prospects or MAX_PROSPECTS), 50))
        self.remember_domains = remember_domains
        self.prospects: list[dict] = []
        self.seen_domains: dict[str, str] = {}
        # The agent's own market commentary, supplied via report_scan_findings.
        self.market_insights: str = ""
        self.search_tips: str = ""
        self.rejected: list[dict] = []

    # -- seen-domain memory -------------------------------------------------

    def load_scanned(self) -> dict[str, str]:
        """Load previously scanned domains, pruning entries older than the TTL."""
        now = datetime.now()
        cutoff = now - timedelta(days=SCAN_EXPIRE_DAYS)
        scanned: dict[str, str] = {}

        if not self.scanned_path.exists():
            self.seen_domains = {}
            return {}

        try:
            raw = json.loads(self.scanned_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("could not read %s: %s", self.scanned_path, exc)
            self.seen_domains = {}
            return {}

        domains = raw.get("domains", []) if isinstance(raw, dict) else raw
        if domains and isinstance(domains[0], str):
            # Legacy format: a bare list of domain strings.
            scanned = {d: raw.get("updated", now.isoformat()) for d in domains}
        else:
            for entry in domains:
                if isinstance(entry, dict) and entry.get("domain"):
                    scanned[entry["domain"]] = entry.get("scanned_at", now.isoformat())

        pruned = 0
        kept: dict[str, str] = {}
        for domain, timestamp in scanned.items():
            try:
                if datetime.fromisoformat(timestamp) > cutoff:
                    kept[domain] = timestamp
                else:
                    pruned += 1
            except (TypeError, ValueError):
                kept[domain] = timestamp   # unparseable: keep rather than lose the memory

        if pruned:
            logger.info("pruned %s domain(s) older than %s days", pruned, SCAN_EXPIRE_DAYS)
        self.seen_domains = kept
        return kept

    def remember(self, domain: str) -> None:
        if domain:
            self.seen_domains[domain] = datetime.now().isoformat()

    def save_scanned(self) -> None:
        payload = {
            "domains": [
                {"domain": d, "scanned_at": ts} for d, ts in sorted(self.seen_domains.items())
            ],
            "updated": datetime.now().isoformat(),
            "total": len(self.seen_domains),
        }
        self.scanned_path.parent.mkdir(parents=True, exist_ok=True)
        self.scanned_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # -- prospects ----------------------------------------------------------

    def add(self, prospect: dict) -> str:
        """Record one evaluated prospect. Returns 'added' or 'skipped'."""
        website = (prospect.get("website") or "").strip()
        company = (prospect.get("company_name") or "").strip()
        if not website or not company:
            return "skipped"
        if len(self.prospects) >= self.max_prospects:
            return "capped"
        if any(p["website"] == website for p in self.prospects):
            return "skipped"

        emails = [e.strip() for e in (prospect.get("emails") or []) if e and "@" in e]
        self.prospects.append(
            {
                "company_name": company[:200],
                "website": website,
                "country": (prospect.get("country") or "").strip(),
                "industry": (prospect.get("industry") or "").strip(),
                "keyword": (prospect.get("keyword") or "").strip(),
                "snippet": (prospect.get("why") or "")[:400],
                "emails": emails[:3],
                "phones": [str(p) for p in (prospect.get("phones") or [])][:2],
                "relevance_score": float(prospect.get("score") or 0),
                "page_text_preview": (prospect.get("evidence") or "")[:500],
                "agent_recommendation": (prospect.get("recommendation") or "").strip(),
                "agent_reasoning": (prospect.get("reasoning") or "")[:800],
                "draft_email": (prospect.get("draft_email") or "").strip(),
                "draft_subject": (
                    prospect.get("draft_subject")
                    or f"Partnership Inquiry - Quality {(prospect.get('keyword') or 'Textile Products')} from China"
                ),
            }
        )
        if self.remember_domains:
            self.remember(_domain_of(website))
        return "added"

    @property
    def count(self) -> int:
        return len(self.prospects)

    # -- output -------------------------------------------------------------

    def write_leads(self, *, keywords: list[str], countries: list[str]) -> Path:
        """Write the JSON the existing ``/agent/import-leads`` endpoint consumes.

        The field names are unchanged on purpose: this is a drop-in replacement
        for the old script, not a new contract.
        """
        out = self.out_dir / "daily_leads.json"
        payload = {
            "scan_date": datetime.now().isoformat(),
            "keywords_used": keywords,
            "countries_scanned": countries,
            "total_found": len(self.prospects),
            "leads": [
                {
                    "company_name": p["company_name"],
                    "website": p["website"],
                    "country": p["country"],
                    "keyword": p["keyword"],
                    "snippet": p["snippet"],
                    "emails": p["emails"],
                    "relevance_score": p["relevance_score"],
                    "page_text_preview": p["page_text_preview"],
                    "draft_email": p["draft_email"],
                    "draft_subject": p["draft_subject"],
                    # Extra fields. The importer ignores unknown keys, but a
                    # human reading the file wants to know why each lead is here.
                    "agent_recommendation": p["agent_recommendation"],
                    "agent_reasoning": p["agent_reasoning"],
                }
                for p in self.prospects
            ],
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return out

    def build_report(self, *, keywords: list[str], countries: list[str],
                     skipped: int, run: Any = None) -> str:
        date = datetime.now().strftime("%Y-%m-%d")
        lines = [
            f"TradeAgent daily report — {date}",
            f"Keywords : {', '.join(keywords)}",
            f"Markets  : {', '.join(countries[:6])}{'...' if len(countries) > 6 else ''}",
            f"Already scanned (skipped): {skipped} domains",
            "=" * 62,
            "",
        ]

        if not self.prospects:
            lines += [
                "[no prospects]",
                "",
                "Nothing cleared the agent's bar today. The run's reasoning is below —",
                "if it judged candidates but rejected them all, that is a real answer.",
                "",
            ]
        else:
            lines.append(f"[{len(self.prospects)} prospect(s)] evaluated and kept by the agent:")
            lines.append("")
        for i, p in enumerate(self.prospects, 1):
            lines.append(f"  {i}. {p['company_name']}  ({p['relevance_score']:.0f}/100)")
            lines.append(f"     {p['website']}")
            if p["country"] or p["industry"]:
                lines.append(f"     {p['country']} · {p['industry']}".rstrip(" ·"))
            lines.append(f"     emails: {', '.join(p['emails']) or 'none found'}")
            lines.append(f"     verdict: {p['agent_recommendation'] or 'n/a'}")
            if p["agent_reasoning"]:
                lines.append("     why:")
                for chunk in _wrap(p["agent_reasoning"], 92):
                    lines.append(f"       {chunk}")
            if p["draft_email"]:
                lines += ["", "     --- DRAFT (NOT SENT — review first) ---"]
                for draft_line in p["draft_email"].splitlines():
                    lines.append(f"     {draft_line}")
                lines.append("     --- END DRAFT ---")
            lines.append("")

        lines += ["=" * 62]
        lines.append("No email was sent to anyone. All drafts are for review only.")

        if self.market_insights:
            lines += ["", "What the agent concluded about the market", "-" * 62]
            lines += [f"  {chunk}" for chunk in _wrap(self.market_insights, 92)]
        if self.search_tips:
            lines += ["", "Search notes for next time", "-" * 62]
            lines += [f"  {chunk}" for chunk in _wrap(self.search_tips, 92)]

        if run is not None:
            lines += ["", "Agent run", "-" * 62]
            lines.append(f"  status      : {run.status} ({run.stop_reason})")
            lines.append(f"  tool path   : {' -> '.join(run.tool_sequence) or '(none)'}")
            lines.append(f"  rounds/calls: {len(run.steps)}/{run.tool_call_count}")
            lines.append(
                f"  tokens      : {run.prompt_tokens} in "
                f"({run.cached_prompt_tokens} cached) / {run.completion_tokens} out"
            )
            lines.append("")
            if run.final_output:
                lines.append("  conclusion:")
                for chunk in _wrap(run.final_output, 92):
                    lines.append(f"    {chunk}")
        lines += ["", "Generated by TradeAgent."]
        return "\n".join(lines)


def _domain_of(url: str) -> str:
    parts = url.split("/")
    return parts[2] if len(parts) > 2 else url


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def build_scan_registry(sink: ProspectSink, *, allow_save: bool = True) -> ToolRegistry:
    """Scrape-side tools plus the sink.

    ``allow_save=False`` gives a read-only registry, used when the run is a
    dry run: the agent can still investigate and report, it just cannot commit
    prospects to the file.
    """
    from .trade_tools import inspect_website, search_prospects

    registry = ToolRegistry()
    registry.register(search_prospects)
    registry.register(inspect_website)
    registry.register(_already_scanned_tool(sink))
    registry.register(_report_findings_tool(sink))

    if allow_save:
        registry.register(_save_prospects_tool(sink))
    return registry


def _report_findings_tool(sink: ProspectSink):
    @tool(
        name="report_scan_findings",
        description=(
            "Record what you learned about the market itself, as opposed to the "
            "individual companies: which buyer types dominate, which phrasings found "
            "real importers, what made candidates fail, which markets look promising. "
            "Call this once, near the end. It is free-text and is shown to the human."
        ),
        tags={"write", "scan"},
    )
    async def report_scan_findings(market_insights: str, search_tips: str = "") -> dict:
        sink.market_insights = (market_insights or "").strip()[:2000]
        sink.search_tips = (search_tips or "").strip()[:1000]
        return {
            "recorded": True,
            "market_insights_chars": len(sink.market_insights),
            "search_tips_chars": len(sink.search_tips),
        }

    return report_scan_findings


def _already_scanned_tool(sink: ProspectSink):
    @tool(
        name="already_scanned",
        description=(
            "Return the domains this scan has already seen, so you can skip them. "
            "Call this before searching if you want to avoid re-inspecting known sites."
        ),
        tags={"read", "scan"},
    )
    async def already_scanned(text: str = "") -> dict:
        # A parameter is required for the derived schema to be valid; it is unused.
        return {
            "count": len(sink.seen_domains),
            "domains": sorted(sink.seen_domains)[:60],
            "expires_after_days": SCAN_EXPIRE_DAYS,
        }

    return already_scanned


def _save_prospects_tool(sink: ProspectSink):
    @tool(
        name="save_prospects",
        description=(
            "Commit the prospects you have judged worth keeping. Call this once, at "
            "the end, with only the candidates you actually inspected and decided to "
            "recommend. A score here is your own judgement, not a computed value. "
            "Nothing is emailed to anyone by this tool."
        ),
        tags={"write", "scan"},
    )
    async def save_prospects(prospects: list[dict]) -> dict:
        """Persist the agent's evaluated verdicts.

        ``prospects`` is a list of objects with:
          company_name (required), website (required), country, industry,
          keyword, why (one line on why they fit), emails (list), score (0-100,
          the agent's own judgement), recommendation
          (approach_now|approach_later|low_priority|not_a_fit), reasoning,
          evidence (excerpt from their site), draft_email, draft_subject.
        """
        results = []
        for item in prospects or []:
            if not isinstance(item, dict):
                results.append("skipped")
                continue
            results.append(sink.add(item))

        return {
            "added": results.count("added"),
            "skipped": results.count("skipped"),
            "capped": results.count("capped"),
            "total_kept": sink.count,
            "max_prospects": sink.max_prospects,
        }

    return save_prospects


def describe_registry(registry: ToolRegistry) -> list[str]:
    return registry.names()
