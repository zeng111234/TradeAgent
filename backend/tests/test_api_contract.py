"""Characterisation tests: the public HTTP surface must not change.

These build the safety net for the agent refactor. Every route that exists
today must still exist afterwards, with the same method and the same required
request shape. If this test has to be edited during the refactor, that is a
breaking change and needs an explicit decision.
"""
from __future__ import annotations

# Snapshot of the API surface as of the commit this refactor started from
# (f8f8836). 42 paths / 54 operations.
EXPECTED_ROUTES: dict[str, set[str]] = {
    "/": {"get"},
    "/health": {"get"},
    # --- agent router (15 operations) ---
    "/api/v1/agent/analyze-email": {"post"},
    "/api/v1/agent/analyze-inquiry": {"post"},
    "/api/v1/agent/analyze-website": {"post"},
    "/api/v1/agent/batch-emails": {"post"},
    "/api/v1/agent/churn-alerts": {"post"},
    "/api/v1/agent/daily-intelligence": {"post"},
    # Read the plan without committing it; the POST above writes the schedule.
    "/api/v1/agent/follow-up-plan": {"get"},
    "/api/v1/agent/generate-pi": {"post"},
    "/api/v1/agent/holiday-emails": {"post"},
    "/api/v1/agent/holidays": {"get"},
    "/api/v1/agent/import-leads": {"post"},
    "/api/v1/agent/import-leads/preview": {"get"},
    "/api/v1/agent/negotiation-advice": {"post"},
    "/api/v1/agent/scan-leads": {"post"},
    "/api/v1/agent/scan-leads-draft": {"post"},
    "/api/v1/agent/send-drafted-emails": {"post"},
    # --- goal-driven agent runs (added by the agent refactor) ---
    "/api/v1/agent/capabilities": {"get"},
    "/api/v1/agent/run": {"post"},
    "/api/v1/agent/runs": {"get"},
    "/api/v1/agent/runs/{run_id}": {"get"},
    "/api/v1/agent/runs/{run_id}/trace.html": {"get"},
    # --- analytics router (5) ---
    "/api/v1/analytics/countries": {"get"},
    "/api/v1/analytics/dashboard": {"get"},
    "/api/v1/analytics/pipeline": {"get"},
    "/api/v1/analytics/sources": {"get"},
    "/api/v1/analytics/trends": {"get"},
    # --- customers router (5) ---
    "/api/v1/customers/": {"get", "post"},
    "/api/v1/customers/import": {"post"},
    "/api/v1/customers/{customer_id}": {"get", "put", "delete"},
    "/api/v1/customers/{customer_id}/contacts": {"get", "post"},
    "/api/v1/customers/{customer_id}/notes": {"get", "post"},
    # One customer's whole story, read-only: see app/services/customer_debug.py.
    "/api/v1/customers/{customer_id}/debug": {"get"},
    # --- emails router (11) ---
    "/api/v1/emails/generate": {"post"},
    "/api/v1/emails/logs": {"get"},
    # NOTE: no GET here. Ordering was mis-read from the router initially;
    # ``emails.py`` only defines PUT (320) and DELETE (341) for this path.
    "/api/v1/emails/logs/{email_id}": {"put", "delete"},
    # The reply and the rejection: the two ways a human decides on a proposal.
    # A rejection is kept rather than deleted, so the next proposal can see it.
    "/api/v1/emails/logs/{email_id}/reply": {"post"},
    "/api/v1/emails/logs/{email_id}/reject": {"post"},
    "/api/v1/emails/send": {"post"},
    "/api/v1/emails/send-pending": {"post"},
    "/api/v1/emails/send-pending/{email_id}": {"post"},
    "/api/v1/emails/stats": {"get"},
    "/api/v1/emails/templates": {"get", "post"},
    "/api/v1/emails/templates/{template_id}": {"get", "put", "delete"},
    "/api/v1/emails/track/{tracking_id}/pixel": {"get"},
    # --- scheduler (3) ---
    "/api/v1/scheduler/jobs": {"get"},
    # What actually ran, including the fires that were skipped rather than
    # stacked on top of a run still in progress.
    "/api/v1/scheduler/history": {"get"},
    "/api/v1/scheduler/run-daily": {"post"},
    # --- tasks router (3) ---
    "/api/v1/tasks/": {"get", "post"},
    "/api/v1/tasks/overdue/list": {"get"},
    "/api/v1/tasks/{task_id}": {"get", "put", "delete"},
}


def _live_routes() -> dict[str, set[str]]:
    from app.main import app
    live: dict[str, set[str]] = {}
    for path, ops in app.openapi()["paths"].items():
        live[path] = {m.lower() for m in ops}
    return live


def test_route_count_unchanged():
    live = _live_routes()
    assert len(live) == len(EXPECTED_ROUTES), (
        "path count changed: "
        f"expected {len(EXPECTED_ROUTES)}, got {len(live)}\n"
        f"added: {sorted(set(live) - set(EXPECTED_ROUTES))}\n"
        f"removed: {sorted(set(EXPECTED_ROUTES) - set(live))}"
    )


def test_no_route_removed_and_no_method_lost():
    live = _live_routes()
    for path, methods in EXPECTED_ROUTES.items():
        assert path in live, f"route removed: {path}"
        missing = methods - live[path]
        assert not missing, f"{path} lost methods: {sorted(missing)}"


def test_root_and_health_contract():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

        r = c.get("/")
        assert r.status_code == 200
        body = r.json()
        # The frontend reads name/version; keep those keys.
        assert "name" in body and "version" in body


def test_unknown_route_is_404_not_500():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        assert c.get("/api/v1/definitely-not-a-route").status_code == 404
