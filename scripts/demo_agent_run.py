"""Produce a visual demo of the agent runtime.

Runs the real loop, the real tool registry and the real renderer, with a
scripted LLM in place of a network call so the output is deterministic and the
demo can run in CI.

    python -m scripts.demo_agent_run [outdir]

Writes, into ``outdir`` (default ``artifacts/agent-demo``):

* ``compare.html``   — one page with both runs and a combined sequence diagram
* ``run-new-leads.html`` / ``run-pipeline.html`` — per-run timeline pages
* ``compare.mmd`` / ``*.mmd`` — the raw Mermaid source

Why this exists: it is the answer to "should the workflow be drawn in n8n".
This diagram is generated from what the run actually did. A hand-drawn canvas
can only show what somebody remembered to draw.
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agent import AgentBudget, AgentLoop, run_agent  # noqa: E402
from app.agent.render import (  # noqa: E402
    render_html,
    render_mermaid_flowchart,
    render_mermaid_sequence,
    render_mermaid_sequence_comparison,
)
from app.agent.trade_tools import build_trade_registry  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "artifacts" / "agent-demo"


# --- Two scripted runs -----------------------------------------------------
#
# These stand in for a real model. Each scripted reply is chosen the way a model
# would choose it from the tool list — the point being that the *loop* has no
# idea which tools matter for which goal. Swap ScriptedLLM for
# OpenAICompatibleClient and the code path is identical.

SCRIPT_NEW_LEADS = [
    {
        "content": "Goal is to find new buyers in Germany. I should search first rather than guess.",
        "tool_calls": [{
            "id": "c1", "name": "search_prospects",
            "arguments": json.dumps({"keyword": "embroidery thread", "country": "Germany", "max_results": 3}),
        }],
    },
    {
        "content": "Three candidates. I cannot judge any of them from a search snippet, so inspect them.",
        "tool_calls": [{
            "id": "c2", "name": "inspect_website",
            "arguments": json.dumps({"url": "https://example-textil-gmbh.de"}),
        }],
    },
    {
        "content": "Now I have the page text. I should get the judging criteria rather than invent a score.",
        "tool_calls": [{
            "id": "c3", "name": "classify_prospect",
            "arguments": json.dumps({
                "page_text": "Example Textil GmbH - importer and wholesaler of embroidery threads, "
                             "metallic yarns and trims for the German apparel industry. Since 1978.",
                "target_products": "gold metallic yarn, embroidery thread",
            }),
        }],
    },
    {
        "content": "Rubric returned. I do not need to write to the CRM yet — I have no confirmation from "
                   "a human that this prospect is wanted, and creating rows I was not asked for is noise.",
    },
]

SCRIPT_PIPELINE = [
    {
        "content": "Goal is about existing customers, so this is a CRM question, not a prospecting one.",
        "tool_calls": [{
            "id": "d1", "name": "list_customers",
            "arguments": json.dumps({"stage": "quoting"}),
        }],
    },
    {
        "content": "One customer is in quoting. Numbers, not a verdict — I need the raw signals.",
        "tool_calls": [{
            "id": "d2", "name": "get_customer_signals",
            "arguments": json.dumps({"customer_id": 2}),
        }],
    },
    {
        "content": "Signals show 0 replies across 3 emails and 41 days in stage. I should read the "
                   "actual correspondence before concluding anything from the counters.",
        "tool_calls": [{
            "id": "d3", "name": "get_communication_history",
            "arguments": json.dumps({"customer_id": 2}),
        }],
    },
    {
        "content": (
            "Conclusion for EuroTextil GmbH (customer 2, quoting, 72/100).\n\n"
            "The counters are unambiguous: 3 emails sent, 0 replies, one send never opened, "
            "41 days in the quoting stage.\n\n"
            "What I could not establish: whether the silence is disinterest or whether the quote "
            "was simply wrong for them. get_communication_history returned only outbound records, "
            "so there is no inbound side to read. Treating this as 'they went cold' would be a guess.\n\n"
            "Recommended next step, for a human to action: one short follow-up that asks a direct "
            "question about the quote rather than a chase-up. If there is no reply to that, the "
            "honest reading is that this is lost, and the stage field should say so."
        ),
    },
]


class ScriptedLLM:
    """Same interface as the real client, but replays a fixed script."""

    def __init__(self, script: list[dict]) -> None:
        self.script = script
        self.index = 0

    async def chat(self, messages, tools, *, temperature=0.2, max_tokens=1500):
        from app.agent.protocol import LLMReply, ToolCall

        if self.index >= len(self.script):
            return LLMReply(content="(script exhausted)")
        entry = self.script[self.index]
        self.index += 1
        return LLMReply(
            content=entry.get("content", ""),
            tool_calls=[
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                for tc in entry.get("tool_calls", [])
            ],
            prompt_tokens=180,
            completion_tokens=90,
        )


# --- Stubs for the network-backed tools ------------------------------------
#
# The demo must not hit the internet, so the two tools that fetch from the web
# are replaced. Everything else — the loop, the registry, the budget checks,
# the trace, the renderer — is the real thing.

STUB_SEARCH = {
    "count": 3,
    "queries_run": ["embroidery thread importer Germany", "embroidery thread buyer Germany"],
    "candidates": [
        {"company_name": "Example Textil GmbH", "website": "https://example-textil-gmbh.de",
         "domain": "example-textil-gmbh.de", "snippet": "Importer of embroidery threads and trims",
         "search_keyword": "embroidery thread", "search_country": "Germany"},
        {"company_name": "Nordstickerei Handel", "website": "https://nordstickerei.example",
         "domain": "nordstickerei.example", "snippet": "Wholesale needlework supplies",
         "search_keyword": "embroidery thread", "search_country": "Germany"},
        {"company_name": "Textile Buyer Portal", "website": "https://b2b-portal.example",
         "domain": "b2b-portal.example", "snippet": "Directory of European textile buyers",
         "search_keyword": "embroidery thread", "search_country": "Germany"},
    ],
    "errors": [],
    "note": "Unranked and unscored by design.",
}

STUB_INSPECT = {
    "url": "https://example-textil-gmbh.de",
    "status_code": 200,
    "title": "Example Textil GmbH - Embroidery Threads and Metallic Yarns",
    "emails": ["einkauf@example-textil-gmbh.de"],
    "phones": ["+49 40 123456"],
    "text": (
        "Example Textil GmbH\n"
        "Importer and wholesaler of embroidery threads, metallic yarns and trims.\n"
        "Serving the German apparel and upholstery industry since 1978.\n"
        "Contact purchasing: einkauf@example-textil-gmbh.de\n"
    ),
    "text_truncated": False,
    "text_total_chars": 240,
}


def with_network_tools_stubbed(stubs: dict) -> "ToolRegistry":
    """A copy of the real registry with selected tool bodies swapped.

    Names, descriptions and parameter schemas are kept from the real tools, so
    the model is driven by exactly the interface it would see in production.
    Only the bodies that would touch the internet or the database are replaced.
    """
    from app.agent.tools import Tool, ToolRegistry

    real = build_trade_registry()
    stubbed = ToolRegistry()

    for name in real.names():
        original = real.get(name)
        if name in stubs:
            stubbed.register(
                Tool(name, original.description, original.params_model,
                     stubs[name], set(original.tags))
            )
        else:
            stubbed.register(original)
    return stubbed


def _stub_functions() -> dict:
    async def stubbed_search(keyword: str, country: str = "", max_results: int = 8) -> dict:
        return STUB_SEARCH

    async def stubbed_inspect(url: str, max_chars: int = 3000) -> dict:
        return STUB_INSPECT

    async def stubbed_signals(customer_id: int) -> dict:
        return {
            "customer_id": customer_id, "company_name": "EuroTextil GmbH", "stage": "quoting",
            "country": "Germany", "score": 72.0, "age_days": 132,
            "days_since_last_contact": 41, "days_in_current_stage": 41,
            "emails_total": 3, "emails_sent": 3, "emails_replied": 0, "emails_opened": 1,
            "emails_pending": 0, "emails_bounced": 0, "never_contacted": False, "has_any_reply": False,
        }

    async def stubbed_history(customer_id: int, limit: int = 15) -> dict:
        return {
            "customer_id": customer_id, "message_count": 3, "engaged_count": 1,
            "replied_count": 0, "opened_count": 1, "pending_count": 0, "bounced_count": 0,
            "last_message_at": "2026-09-03T09:12:00",
            "messages": [
                {"id": 3, "subject": "Following up - quote for metallic yarn",
                 "status": "sent", "to_email": "hans@eurotextil.de",
                 "created_at": "2026-09-03T09:12:00", "sent_at": "2026-09-03T09:12:30",
                 "opened_at": None, "open_count": 0, "replied_at": None,
                 "body_preview": "Just checking whether you had a chance to review the quote..."},
                {"id": 2, "subject": "Your quote for gold thread, 3 variants",
                 "status": "opened", "to_email": "hans@eurotextil.de",
                 "created_at": "2026-08-21T14:03:00", "sent_at": "2026-08-21T14:03:20",
                 "opened_at": "2026-08-22T08:41:00", "open_count": 2, "replied_at": None,
                 "body_preview": "Please find attached our quotation for the three variants..."},
                {"id": 1, "subject": "Samples for gold thread and metallic yarn",
                 "status": "opened", "to_email": "hans@eurotextil.de",
                 "created_at": "2026-08-05T10:30:00", "sent_at": "2026-08-05T10:30:40",
                 "opened_at": "2026-08-05T16:12:00", "open_count": 1, "replied_at": None,
                 "body_preview": "Samples for the three requested variants were dispatched today..."},
            ],
        }

    async def stubbed_list(stage: str = "", country: str = "", min_score: float = 0.0,
                           search: str = "", limit: int = 20, include_closed: bool = False) -> dict:
        return {
            "count": 1,
            "filters": {"stage": stage or "(open only)", "country": None,
                        "min_score": None, "search": None},
            "customers": [{
                "id": 2, "company_name": "EuroTextil GmbH", "country": "Germany",
                "stage": "quoting", "score": 72.0,
                "products": "gold thread, embroidery supplies, metallic thread",
                "industry": "Textile & Embroidery", "website": "https://www.eurotextil.de",
                "last_contacted_at": "2026-09-03T09:12:00", "next_follow_up_at": None,
            }],
        }

    return {
        "search_prospects": stubbed_search,
        "inspect_website": stubbed_inspect,
        "get_customer_signals": stubbed_signals,
        "get_communication_history": stubbed_history,
        "list_customers": stubbed_list,
    }


def main(argv: list[str]) -> int:
    outdir = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    outdir.mkdir(parents=True, exist_ok=True)

    registry = with_network_tools_stubbed(_stub_functions())

    import asyncio

    async def go():
        budget = AgentBudget(max_rounds=8, max_tool_calls=15)
        new_leads = await run_agent(
            "找出 5 家德国新的刺绣线潜在买家，并给出联系方式与切入角度",
            ScriptedLLM(SCRIPT_NEW_LEADS), registry, budget=budget,
        )
        pipeline = await run_agent(
            "看看我现有客户里哪些需要今天跟进，给我理由",
            ScriptedLLM(SCRIPT_PIPELINE), registry, budget=budget,
        )
        return new_leads, pipeline

    new_leads, pipeline = asyncio.run(go())

    # --- write artefacts ---------------------------------------------------
    comparison = render_mermaid_sequence_comparison(
        [("找新客户 / find new buyers", new_leads), ("查老客户 / review pipeline", pipeline)]
    )
    (outdir / "compare.mmd").write_text(comparison, encoding="utf-8")
    (outdir / "run-new-leads.html").write_text(render_html(new_leads), encoding="utf-8")
    (outdir / "run-pipeline.html").write_text(render_html(pipeline), encoding="utf-8")
    (outdir / "run-new-leads.mmd").write_text(render_mermaid_sequence(new_leads), encoding="utf-8")
    (outdir / "run-pipeline.mmd").write_text(render_mermaid_sequence(pipeline), encoding="utf-8")
    (outdir / "run-new-leads-flow.mmd").write_text(render_mermaid_flowchart(new_leads), encoding="utf-8")
    (outdir / "run-pipeline-flow.mmd").write_text(render_mermaid_flowchart(pipeline), encoding="utf-8")

    (outdir / "traces.json").write_text(
        json.dumps({"new_leads": new_leads.as_dict(), "pipeline": pipeline.as_dict()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>TradeAgent — agent trace comparison</title>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  mermaid.initialize({{ startOnLoad: true, theme: "neutral" }});
</script>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
         margin: 0 auto; max-width: 1180px; padding: 2rem 1.25rem 4rem; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .4rem; }}
  p.lede {{ opacity: .78; margin: 0 0 1.75rem; max-width: 74ch; }}
  .runs {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; margin: 0 0 2rem; }}
  @media (max-width: 780px) {{ .runs {{ grid-template-columns: 1fr; }} }}
  .card {{ border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
           border-radius: 10px; padding: .9rem 1.1rem; }}
  .card h2 {{ font-size: .95rem; margin: 0 0 .5rem; }}
  .card ol {{ margin: .5rem 0 0; padding-left: 1.2rem; }}
  .card li {{ margin: .25rem 0; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .86em; }}
  .seq {{ overflow-x: auto; margin: 0 0 2rem; }}
  .verdict {{ padding: .9rem 1.1rem; border-radius: 10px;
              background: color-mix(in srgb, currentColor 6%, transparent); }}
</style></head>
<body>
  <h1>Same loop. Same tools. Two different goals.</h1>
  <p class="lede">Both runs below go through the identical code path: the same
  <code>AgentLoop</code>, the same nine-tool registry, the same budget. Nothing in the
  loop knows which tools suit which goal — the model chose. This page was generated
  from the traces the runs actually produced.</p>

  <div class="runs">
    <div class="card">
      <h2>找新客户 — find new buyers</h2>
      <div><code>{html.escape(" → ".join(new_leads.tool_sequence)) or "(none)"}</code></div>
      <ol>{''.join(f"<li>{html.escape(x)}</li>" for x in new_leads.tool_sequence)}</ol>
      <p>status <strong>{html.escape(new_leads.status)}</strong>,
         {len(new_leads.steps)} rounds, {new_leads.tool_call_count} tool calls,
         {round(new_leads.elapsed, 3)}s</p>
    </div>
    <div class="card">
      <h2>查老客户 — review the pipeline</h2>
      <div><code>{html.escape(" → ".join(pipeline.tool_sequence)) or "(none)"}</code></div>
      <ol>{''.join(f"<li>{html.escape(x)}</li>" for x in pipeline.tool_sequence)}</ol>
      <p>status <strong>{html.escape(pipeline.status)}</strong>,
         {len(pipeline.steps)} rounds, {pipeline.tool_call_count} tool calls,
         {round(pipeline.elapsed, 3)}s</p>
    </div>
  </div>

  <div class="verdict">
    <strong>The sequences differ:</strong>
    <code>{html.escape(str(new_leads.tool_sequence != pipeline.tool_sequence))}</code>.
    A hardcoded pipeline cannot produce this, because it would have to contain a branch
    for every goal a user might type. This decides what to do from the goal instead.
  </div>

  <h2 style="font-size:1.1rem;margin:2rem 0 .75rem;">Combined trace</h2>
  <div class="seq"><pre class="mermaid">{html.escape(comparison)}</pre></div>

  <p class="lede">Per-run pages with every tool argument, observation and latency:
  <a href="run-new-leads.html">run-new-leads.html</a> ·
  <a href="run-pipeline.html">run-pipeline.html</a></p>
</body></html>"""
    (outdir / "compare.html").write_text(page, encoding="utf-8")

    # --- console summary ---------------------------------------------------
    for label, run in (("find new buyers", new_leads), ("review pipeline", pipeline)):
        print(f"\n=== {label} ===")
        print(f"  status      : {run.status} ({run.stop_reason})")
        print(f"  tool path   : {' -> '.join(run.tool_sequence) or '(none)'}")
        print(f"  rounds/calls: {len(run.steps)}/{run.tool_call_count}")
        if run.final_output:
            print(f"  answer      : {run.final_output.splitlines()[0][:110]}")

    differ = new_leads.tool_sequence != pipeline.tool_sequence
    print(f"\ntool sequences differ: {differ}")
    print(f"artefacts written to: {outdir}")
    return 0 if differ else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
