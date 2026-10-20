"""The daily scan, run by the agent.

These tests exercise the replacement for the hardcoded scan loop: the real
``AgentLoop`` and the real ``ProspectSink``, with a scripted model and the two
network tools stubbed.
"""
from __future__ import annotations

import json

import pytest

from app.agent.scan_runner import build_scan_goal, find_leads_agentic, run_scan
from app.agent.scan_tools import ProspectSink, build_scan_registry
from tests.scripted_llm import CANNED_PAGE, ScriptedLLM, call, final, registry_with_stubs, scan_stubs


def _save(*prospects) -> dict:
    return call("save_prospects", {"prospects": list(prospects)})


PROSPECT_A = {
    "company_name": "Example Textil GmbH",
    "website": "https://example-textil.example",
    "country": "Germany",
    "industry": "Textile",
    "keyword": "embroidery thread",
    "why": "Imports embroidery threads for the German apparel market",
    "emails": ["einkauf@example-textil.example"],
    "score": 82,
    "recommendation": "approach_now",
    "reasoning": "Site explicitly describes importing and wholesaling embroidery threads.",
    "evidence": "Importer and wholesaler of embroidery threads and metallic yarns.",
}

PROSPECT_B = {
    "company_name": "Nordstickerei Handel",
    "website": "https://nordstickerei.example",
    "country": "Germany",
    "industry": "Needlework",
    "why": "Wholesale needlework supplies",
    "emails": [],
    "score": 55,
    "recommendation": "approach_later",
    "reasoning": "Related category but no contact email found on the page.",
}


async def _run_with(tmp_path, script, *, headcount=5, commit=True, search=None, inspect=None):
    """Run the scan with the network tools stubbed.

    ``scan_runner`` does ``from .scan_tools import build_scan_registry``, so the
    name is bound in scan_runner's namespace — patching scan_tools would have no
    effect. It must be patched where it is looked up.
    """
    import app.agent.scan_runner as runner
    import app.agent.scan_tools as scan_tools

    real_build = scan_tools.build_scan_registry

    def patched_build(sink, *, allow_save=True):
        return registry_with_stubs(real_build(sink, allow_save=allow_save),
                                   scan_stubs(search=search, inspect=inspect))

    runner.build_scan_registry = patched_build
    try:
        return await run_scan(
            keywords=["embroidery thread"],
            countries=["Germany"],
            out_dir=tmp_path,
            headcount=headcount,
            llm=ScriptedLLM(script),
            commit=commit,
        )
    finally:
        runner.build_scan_registry = real_build


# --- the happy path --------------------------------------------------------

async def test_agent_driven_scan_writes_leads_file(tmp_path):
    outcome = await _run_with(
        tmp_path,
        [
            call("already_scanned", {}),
            call("search_prospects", {"keyword": "embroidery thread", "country": "Germany"}),
            call("inspect_website", {"url": "https://example-textil.example"}),
            _save(PROSPECT_A),
            call("report_scan_findings", {
                "market_insights": "German importers describe themselves as wholesalers.",
                "search_tips": "'importer' outperformed 'buyer'.",
            }),
            final("Kept 1 prospect; rejected the directory page."),
        ],
    )

    assert outcome.run.status == "succeeded"
    assert outcome.prospect_count == 1
    assert outcome.leads_path is not None and outcome.leads_path.exists()

    payload = json.loads(outcome.leads_path.read_text(encoding="utf-8"))
    assert payload["total_found"] == 1
    assert payload["keywords_used"] == ["embroidery thread"]
    assert payload["countries_scanned"] == ["Germany"]

    lead = payload["leads"][0]
    # Field names the existing /agent/import-leads endpoint reads.
    for key in ("company_name", "website", "country", "keyword", "snippet",
                "emails", "relevance_score", "page_text_preview",
                "draft_email", "draft_subject"):
        assert key in lead, f"missing key the importer needs: {key}"
    assert lead["company_name"] == "Example Textil GmbH"
    assert lead["emails"] == ["einkauf@example-textil.example"]
    assert lead["relevance_score"] == 82
    # The agent's own reasoning travels with the lead.
    assert lead["agent_recommendation"] == "approach_now"
    assert "importing" in lead["agent_reasoning"]


async def test_scan_records_market_findings_and_domain_memory(tmp_path):
    outcome = await _run_with(
        tmp_path,
        [
            call("search_prospects", {"keyword": "embroidery thread"}),
            call("inspect_website", {"url": "https://example-textil.example"}),
            _save(PROSPECT_A),
            call("report_scan_findings", {
                "market_insights": "Importer sites are usually single-page catalogues.",
                "search_tips": "Try 'wholesaler' before 'buyer'.",
            }),
            final("Done."),
        ],
    )

    assert outcome.run.status == "succeeded", outcome.run.stop_reason
    assert outcome.sink.market_insights.startswith("Importer sites")
    assert "wholesaler" in outcome.sink.search_tips
    # The human must actually see the agent's market read, not just the leads.
    assert "Importer sites" in outcome.report
    assert "wholesaler" in outcome.report
    assert "What the agent concluded about the market" in outcome.report

    # Inspected domains are remembered so tomorrow's run can skip them.
    memory = json.loads((tmp_path / "scanned_domains.json").read_text(encoding="utf-8"))
    domains = [d["domain"] for d in memory["domains"]]
    assert "example-textil.example" in domains


async def test_previously_scanned_domains_are_recalled(tmp_path):
    (tmp_path / "scanned_domains.json").write_text(
        json.dumps({"domains": [{"domain": "already.example", "scanned_at": "2099-01-01T00:00:00"}]}),
        encoding="utf-8",
    )
    outcome = await _run_with(
        tmp_path,
        [call("already_scanned", {}), final("Nothing new today, everything was already seen.")],
    )
    assert outcome.prospect_count == 0
    assert "already.example" in outcome.sink.seen_domains


async def test_the_agent_may_legitimately_keep_nothing(tmp_path):
    """An empty result with a reason is a correct result, not a failure."""
    outcome = await _run_with(
        tmp_path,
        [
            call("search_prospects", {"keyword": "embroidery thread"}),
            _save(),      # explicitly saves an empty list
            final("Every candidate was a directory page or a competitor manufacturer. Kept none."),
        ],
    )
    assert outcome.run.status == "succeeded"
    assert outcome.prospect_count == 0
    assert outcome.leads_path.exists()
    assert json.loads(outcome.leads_path.read_text(encoding="utf-8"))["leads"] == []
    assert "no prospects" in outcome.report


# --- caps and safety -------------------------------------------------------

async def test_prospect_cap_is_enforced_by_python_not_the_model(tmp_path):
    many = [dict(PROSPECT_A, company_name=f"Co {i}", website=f"https://co{i}.example")
            for i in range(10)]
    outcome = await _run_with(tmp_path, [_save(*many), final("Saved all ten.")], headcount=3)

    assert outcome.prospect_count == 3, "Python must cap this; the model cannot be trusted to"
    assert len(outcome.kept) == 3


async def test_duplicate_websites_are_not_stored_twice(tmp_path):
    outcome = await _run_with(
        tmp_path,
        [_save(PROSPECT_A, dict(PROSPECT_A, company_name="Same Site Again")), final("Done.")],
    )
    assert outcome.prospect_count == 1


async def test_prospects_without_a_name_or_website_are_rejected(tmp_path):
    outcome = await _run_with(
        tmp_path,
        [
            _save(
                {"company_name": "", "website": "https://x.example"},
                {"company_name": "No Site Co", "website": ""},
                PROSPECT_A,
            ),
            final("Done."),
        ],
    )
    assert outcome.prospect_count == 1


async def test_dry_run_leaves_the_previous_result_intact(tmp_path):
    """A bad model day must not overwrite a good previous result.

    A dry run builds a read-only registry, so `save_prospects` is not offered at
    all — the agent can still search, read and report, it simply cannot commit.
    """
    existing = tmp_path / "daily_leads.json"
    existing.write_text('{"leads": [{"company_name": "yesterday"}]}', encoding="utf-8")

    outcome = await _run_with(
        tmp_path,
        [
            call("search_prospects", {"keyword": "embroidery thread"}),
            call("inspect_website", {"url": "https://example-textil.example"}),
            call("report_scan_findings", {"market_insights": "Would keep one."}),
            final("I would keep Example Textil but nothing was written."),
        ],
        commit=False,
    )

    assert outcome.leads_path is None
    assert outcome.prospect_count == 0
    assert outcome.sink.market_insights == "Would keep one."
    # Yesterday's good result survives.
    assert "yesterday" in existing.read_text(encoding="utf-8")
    assert not (tmp_path / "scanned_domains.json").exists()


async def test_dry_run_registry_omits_the_save_tool(tmp_path):
    """The boundary is enforced by omission, not by asking the model nicely."""
    sink = ProspectSink(out_dir=tmp_path, scanned_path=tmp_path / "scanned.json")
    read_only = build_scan_registry(sink, allow_save=False)
    writable = build_scan_registry(sink, allow_save=True)

    assert "save_prospects" not in read_only
    assert "save_prospects" in writable
    for name in ("search_prospects", "inspect_website", "already_scanned"):
        assert name in read_only


async def test_read_only_registry_cannot_save(tmp_path):
    sink = ProspectSink(out_dir=tmp_path, scanned_path=tmp_path / "scanned.json")
    registry = build_scan_registry(sink, allow_save=False)
    assert "save_prospects" not in registry
    assert "search_prospects" in registry
    assert "inspect_website" in registry


# --- failing runs ----------------------------------------------------------

async def test_a_failed_run_still_produces_a_report(tmp_path):
    class Boom:
        async def chat(self, messages, tools, **kwargs):
            raise RuntimeError("model unavailable")

    import app.agent.scan_runner as runner
    import app.agent.scan_tools as scan_tools

    real_build = scan_tools.build_scan_registry
    runner.build_scan_registry = lambda sink, allow_save=True: registry_with_stubs(
        real_build(sink, allow_save=allow_save), scan_stubs()
    )
    try:
        outcome = await run_scan(
            keywords=["embroidery thread"], countries=["Germany"],
            out_dir=tmp_path, llm=Boom(), commit=True,
        )
    finally:
        runner.build_scan_registry = real_build

    assert outcome.run.status == "error"
    assert outcome.prospect_count == 0
    assert "error" in outcome.report.lower() or "Agent run" in outcome.report
    # The file is still written (empty), so downstream code does not read stale data.
    assert outcome.leads_path.exists()


# --- the goal handed to the agent -----------------------------------------

def test_goal_states_markets_products_and_limit():
    goal = build_scan_goal(["embroidery thread"], ["Germany", "France"], headcount=4)
    assert "4" in goal
    assert "embroidery thread" in goal
    assert "Germany" in goal and "France" in goal
    # The goal must not prescribe a procedure, or we are back to a pipeline.
    assert "search_prospects" not in goal
    assert "inspect_website" not in goal


def test_goal_survives_empty_inputs():
    goal = build_scan_goal([], [])
    assert goal.strip()
    assert "any market" in goal


# --- the legacy adapter ----------------------------------------------------

async def test_find_leads_agentic_returns_the_legacy_shape(tmp_path):
    import app.agent.scan_runner as runner
    import app.agent.scan_tools as scan_tools

    real_build = scan_tools.build_scan_registry
    runner.build_scan_registry = lambda sink, allow_save=True: registry_with_stubs(
        real_build(sink, allow_save=allow_save), scan_stubs()
    )
    try:
        result = await find_leads_agentic(
            product_keywords="embroidery thread",
            target_country="Germany",
            max_results=5,
            llm=ScriptedLLM([
                call("search_prospects", {"keyword": "embroidery thread"}),
                call("inspect_website", {"url": "https://example-textil.example"}),
                _save(PROSPECT_A),
                call("report_scan_findings", {"market_insights": "Importers dominate."}),
                final("Kept one."),
            ]),
        )
    finally:
        runner.build_scan_registry = real_build

    # Keys the existing endpoint and frontend rely on.
    for key in ("total_found", "leads", "market_insights", "search_tips",
                "ai_powered", "search_queries_used"):
        assert key in result, f"lost key: {key}"

    assert result["total_found"] == 1
    lead = result["leads"][0]
    for key in ("company_name", "website", "country", "emails", "phones",
                "relevance_score", "page_title", "snippet", "has_contact",
                "ai_priority", "ai_reason", "approach_angle", "estimated_potential"):
        assert key in lead, f"lost lead key: {key}"

    assert lead["ai_priority"] == "high"       # approach_now maps to high
    assert lead["has_contact"] is True
    assert result["market_insights"] == "Importers dominate."
    # The run is reported so a caller can inspect what actually happened.
    assert result["agent_run"]["status"] == "succeeded"
    assert "inspect_website" in result["agent_run"]["tool_sequence"]


async def test_find_leads_agentic_rejects_empty_keywords():
    with pytest.raises(ValueError):
        await find_leads_agentic(product_keywords="  ", llm=ScriptedLLM([]))


async def test_auto_lead_scanner_delegates_to_the_agent(tmp_path, monkeypatch):
    """The service function must no longer run its own scoring loop."""
    import app.services.agent_service as svc
    from app.agent import scan_runner

    called = {}

    async def fake_find(**kwargs):
        called.update(kwargs)
        return {"total_found": 0, "leads": [], "ai_powered": True}

    monkeypatch.setattr(scan_runner, "find_leads_agentic", fake_find, raising=True)
    result = await svc.auto_lead_scanner(
        product_keywords="embroidery thread", target_country="Germany", max_results=7
    )

    assert called["product_keywords"] == "embroidery thread"
    assert called["max_results"] == 7
    assert result["total_found"] == 0


async def test_auto_lead_scanner_raises_when_no_model_is_configured(monkeypatch):
    """No silent fallback: a scan that cannot judge is not a scan."""
    from app.agent.scan_runner import find_leads_agentic
    from app.agent.service import AgentUnavailable

    # The autouse conftest fixture clears OPENAI_API_KEY, so this is the
    # unconfigured deployment path.
    with pytest.raises(AgentUnavailable) as excinfo:
        await find_leads_agentic(product_keywords="embroidery thread")
    assert "OPENAI_API_KEY" in str(excinfo.value)


async def test_auto_lead_scanner_propagates_the_unavailable_signal(monkeypatch):
    """The service layer must not swallow AgentUnavailable into an empty list.

    Returning ``{"leads": []}`` would render as "no prospects found today",
    which is a different and misleading claim from "no model is configured".
    """
    import app.services.agent_service as svc
    from app.agent import scan_runner
    from app.agent.service import AgentUnavailable

    async def refuse(**kwargs):
        raise AgentUnavailable("No LLM is configured.")

    monkeypatch.setattr(scan_runner, "find_leads_agentic", refuse, raising=True)

    with pytest.raises(AgentUnavailable):
        await svc.auto_lead_scanner(product_keywords="embroidery thread")


# --- when the outside world is down ---------------------------------------
# The search tool treats a failed query as data: it keeps going and reports
# the error, so a total outage arrives as a successful run with zero
# prospects. That is the right behaviour for one flaky query and the wrong
# report for a dead engine -- "nothing cleared the bar" is a claim about the
# market, and nobody looked. These pin the distinction.

FAILED_SEARCH = {
    "count": 0,
    "queries_run": ["embroidery thread importer Germany"],
    "candidates": [],
    "errors": ["embroidery thread importer Germany: ConnectError: all nameservers failed"],
    "note": "Unranked and unscored by design.",
}


async def test_a_dead_search_engine_is_not_reported_as_a_quiet_market(tmp_path):
    outcome = await _run_with(
        tmp_path,
        [
            call("search_prospects", {"keyword": "embroidery thread", "country": "Germany"}),
            final("I could not search; no candidates were evaluated."),
        ],
        search=FAILED_SEARCH,
    )

    assert outcome.search_errors, "the trace holds the tool's own error list"
    assert outcome.search_failed_entirely is True
    assert "THE SEARCH FAILED" in outcome.report
    assert "Nothing cleared the agent's bar" not in outcome.report, (
        "that sentence is a verdict about a market nobody could look at"
    )
    assert "all nameservers failed" in outcome.report, (
        "the cause must reach the human, not just the fact"
    )


async def test_a_partial_search_failure_is_listed_without_crying_outage(tmp_path):
    from tests.scripted_llm import CANNED_SEARCH

    partial = dict(CANNED_SEARCH)
    partial["errors"] = ["embroidery thread buyer Germany: timeout"]

    outcome = await _run_with(
        tmp_path,
        [
            call("search_prospects", {"keyword": "embroidery thread", "country": "Germany"}),
            call("inspect_website", {"url": "https://example-textil.example"}),
            _save(PROSPECT_A),
            final("Kept 1; one query timed out."),
        ],
        search=partial,
    )

    assert outcome.prospect_count == 1
    assert outcome.search_errors == ["embroidery thread buyer Germany: timeout"]
    assert outcome.search_failed_entirely is False, (
        "one failed query among successful ones is a footnote, not an outage"
    )
    assert "External failures" in outcome.report
    assert "THE SEARCH FAILED" not in outcome.report
    assert "the run continued" in outcome.report


async def test_a_page_fetch_outage_is_not_a_verdict_on_the_companies(tmp_path):
    """Search worked, every site was unreachable: the zero is about the network.

    ``inspect_website`` reports a failed fetch as an error the agent must
    reject rather than guess at -- correct for one dead site. When all of them
    are dead, "nothing cleared the bar" would be a judgement about companies
    nobody could open.
    """
    outcome = await _run_with(
        tmp_path,
        [
            call("search_prospects", {"keyword": "embroidery thread", "country": "Germany"}),
            call("inspect_website", {"url": "https://example-textil.example"}),
            final("Every site was unreachable; nothing was evaluated."),
        ],
        inspect={"error": "could not fetch https://example-textil.example: ConnectionError: refused"},
    )

    assert outcome.inspect_errors, "the trace holds the fetch errors"
    assert outcome.search_errors == []
    assert outcome.inspection_failed_entirely is True
    assert "EVERY PAGE FETCH FAILED" in outcome.report
    assert "Nothing cleared the agent's bar" not in outcome.report, (
        "that sentence claims a judgement about companies nobody opened"
    )
    assert "ConnectionError" in outcome.report


async def test_the_interactive_lookup_says_when_the_engine_was_down(tmp_path):
    """``POST /agent/scan-leads`` must not return a bare zero on an outage.

    The endpoint's shape is kept for the frontend, but a caller that only reads
    ``total_found`` would conclude "no buyers in Germany" when the truth is
    "the search never ran".
    """
    import app.agent.scan_runner as runner
    import app.agent.scan_tools as scan_tools

    real_build = scan_tools.build_scan_registry
    runner.build_scan_registry = lambda sink, allow_save=True: registry_with_stubs(
        real_build(sink, allow_save=allow_save), scan_stubs(search=FAILED_SEARCH)
    )
    try:
        result = await find_leads_agentic(
            product_keywords="embroidery thread",
            target_country="Germany",
            max_results=5,
            llm=ScriptedLLM([
                call("search_prospects", {"keyword": "embroidery thread"}),
                final("Search failed; nothing evaluated."),
            ]),
        )
    finally:
        runner.build_scan_registry = real_build

    assert result["total_found"] == 0
    errors = result["external_errors"]
    assert errors["search"], "the failure must travel with the zero"
    assert errors["search_failed_entirely"] is True
    assert "all nameservers failed" in errors["search"][0]
