"""HTTP surface for goal-driven agent runs."""
from __future__ import annotations

import pytest

from app.agent.loop import AgentBudget, run_agent
from app.agent.repository import save_run
from app.agent.service import build_registry
from tests.scripted_llm import ScriptedLLM, call, final


# --- capabilities ----------------------------------------------------------

def test_capabilities_declares_the_boundary(client):
    body = client.get("/api/v1/agent/capabilities").json()

    assert body["tool_count"] >= 9
    assert isinstance(body["tools"], list)
    assert body["configured"] is False      # conftest clears the API key

    # The point of this endpoint: an operator can confirm the boundary without
    # reading the source.
    assert any("send" in item for item in body["cannot"])
    # Every write tool is named here on purpose: adding one is a boundary
    # decision, and this assertion is where it has to be said out loud.
    assert body["write_capable"] == [
        "add_customer_contact",
        "add_customer_note",
        "complete_task",
        "create_email_draft",
        "create_email_template",
        "create_task",
        # Memory: writes and deletes, but only inside the agent's own notebook.
        "forget",
        "reject_email_draft",
        "remember",
        "reschedule_task",
        "update_customer_stage",
        "upsert_customer",
    ]

    names = [t["name"] for t in body["tools"]]
    for forbidden in ("send", "dispatch", "delete"):
        assert not any(forbidden in n for n in names), f"a {forbidden} tool is exposed"


def test_capabilities_lists_required_parameters(client):
    body = client.get("/api/v1/agent/capabilities").json()
    by_name = {t["name"]: t for t in body["tools"]}

    assert by_name["get_customer"]["required"] == ["customer_id"]
    assert "customer_id" in by_name["get_customer"]["parameters"]
    # search_prospects only requires the keyword.
    assert by_name["search_prospects"]["required"] == ["keyword"]


# --- POST /agent/run -------------------------------------------------------

def test_run_refuses_when_no_model_is_configured(client):
    """503, not a fabricated result. Silence here would be the old bug."""
    response = client.post("/api/v1/agent/run", json={"goal": "review my pipeline"})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "OPENAI_API_KEY" in detail
    assert "no fallback" in detail.lower()


def test_run_validates_the_goal(client):
    assert client.post("/api/v1/agent/run", json={"goal": ""}).status_code == 422
    assert client.post("/api/v1/agent/run", json={}).status_code == 422
    # Out-of-range budgets must be rejected rather than silently clamped.
    assert client.post("/api/v1/agent/run",
                       json={"goal": "x", "max_rounds": 0}).status_code == 422
    assert client.post("/api/v1/agent/run",
                       json={"goal": "x", "max_rounds": 999}).status_code == 422


def test_run_returns_the_trace_when_configured(client, monkeypatch):
    """With a model in place the endpoint returns the path, not just the answer."""
    import app.routers.agent_runs as routes

    async def fake_execute(goal, *, context="", budget=None, **kwargs):
        from app.agent.render import render_html, render_mermaid_flowchart, render_mermaid_sequence
        from app.agent.service import AgentResult

        run = await run_agent(
            goal, ScriptedLLM([
                call("list_customers", {"stage": "quoting"}),
                final("One customer is in quoting and has had no reply for 41 days."),
            ]),
            build_registry(),
            budget=AgentBudget(max_rounds=4),
        )
        return AgentResult(
            run=run,
            mermaid_sequence=render_mermaid_sequence(run),
            mermaid_flowchart=render_mermaid_flowchart(run),
            html=render_html(run),
        )

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", fake_execute)

    response = client.post(
        "/api/v1/agent/run", json={"goal": "review my pipeline", "wait": True}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "succeeded"
    assert body["tool_sequence"] == ["list_customers"]
    assert body["rounds"] == 2
    assert body["did_investigate"] is True
    assert body["mermaid_sequence"].startswith("---\ntitle:")
    assert "list_customers" in body["mermaid_sequence"]
    assert body["steps"]
    assert body["steps"][0]["tool_calls"][0]["name"] == "list_customers"


# --- GET /agent/runs -------------------------------------------------------

def test_runs_list_is_empty_initially(client):
    """The envelope is right, and the count agrees with the rows.

    This used to assert ``count == 0`` outright. That claim only held while no
    other test in the session ran an agent -- now that runs can be started in
    the background, a finished one is a legitimate row to find here, and an
    assertion that depends on which test ran first is not a fact about the API.
    """
    body = client.get("/api/v1/agent/runs").json()
    assert set(body) == {"count", "runs"}
    assert body["count"] == len(body["runs"])


async def test_runs_list_and_detail_round_trip(client, db):
    run = await run_agent(
        "look at the pipeline",
        ScriptedLLM([call("list_customers", {}), final("Nothing needs attention.")]),
        build_registry(),
        budget=AgentBudget(max_rounds=4),
    )
    await save_run(db, run)

    listing = client.get("/api/v1/agent/runs").json()
    assert listing["count"] >= 1
    entry = next(r for r in listing["runs"] if r["run_id"] == run.run_id)
    assert entry["status"] == "succeeded"
    assert entry["tool_calls"] == 1
    # Which instructions produced it, on both the list and the detail. A row
    # without this cannot be compared with a run from another day.
    assert entry["prompt_version"] == run.prompt_version

    detail = client.get(f"/api/v1/agent/runs/{run.run_id}").json()
    assert detail["goal"] == "look at the pipeline"
    assert detail["prompt_version"] == run.prompt_version
    assert detail["steps"][0]["tool_calls"][0]["name"] == "list_customers"
    assert detail["mermaid_sequence"].startswith("---\ntitle:")


async def test_run_detail_404_for_unknown_id(client):
    assert client.get("/api/v1/agent/runs/does-not-exist").status_code == 404


async def test_trace_html_is_a_self_contained_page(client, db):
    run = await run_agent(
        "check the pipeline",
        ScriptedLLM([call("list_customers", {}), final("All quiet.")]),
        build_registry(),
        budget=AgentBudget(max_rounds=4),
    )
    await save_run(db, run)

    response = client.get(f"/api/v1/agent/runs/{run.run_id}/trace.html")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    page = response.text
    assert page.startswith("<!DOCTYPE html>")
    assert 'class="mermaid"' in page
    assert "list_customers" in page
    assert 'mermaid@11' in page       # renders without the frontend


async def test_trace_html_404_for_unknown_id(client):
    assert client.get("/api/v1/agent/runs/nope/trace.html").status_code == 404


async def test_trace_html_escapes_hostile_observations(client, db):
    """Trace pages show scraped web text; it must not become executable."""
    from app.agent.tools import ToolRegistry, tool

    reg = ToolRegistry()

    @tool(description="returns hostile html")
    async def hostile() -> dict:
        return {"text": "<script>alert('xss')</script>"}

    reg.register(hostile)
    run = await run_agent(
        "<img src=x onerror=alert(1)>",
        ScriptedLLM([call("hostile", {}), final("done")]),
        reg,
        budget=AgentBudget(max_rounds=3),
    )
    await save_run(db, run)

    page = client.get(f"/api/v1/agent/runs/{run.run_id}/trace.html").text
    assert "<script>alert('xss')</script>" not in page
    assert "&lt;script&gt;" in page
    assert "<img src=x onerror=alert(1)>" not in page
