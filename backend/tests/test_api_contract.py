"""Characterisation tests: the public HTTP surface must not change.

These build the safety net for the agent refactor. Every route that exists
today must still exist afterwards, with the same method and the same required
request shape. If this test has to be edited during the refactor, that is a
breaking change and needs an explicit decision.
"""
from __future__ import annotations

# Snapshot of the API surface. The original snapshot, taken at the commit this
# refactor started from (f8f8836), was 42 paths / 54 operations; the table has
# grown since (the follow-up plan's read-only GET, the customer debug view, the
# scheduler history, /usage -- each addition marked below). It now stands at
# 55 paths / 67 operations, and ``test_route_count_unchanged`` is what keeps
# that number honest: the declared table and the live app must agree exactly.
# The operational quality report (GET /analytics/quality) brings it to
# 57 paths / 69 operations.
EXPECTED_ROUTES: dict[str, set[str]] = {
    "/": {"get"},
    "/health": {"get"},
    "/ready": {"get"},
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
    "/api/v1/agent/runs/{run_id}/events": {"get"},
    "/api/v1/agent/runs/{run_id}/trace.html": {"get"},
    # --- analytics router (6) ---
    "/api/v1/analytics/countries": {"get"},
    "/api/v1/analytics/dashboard": {"get"},
    "/api/v1/analytics/pipeline": {"get"},
    # Operational trend, not business analytics: run/job outcomes and latency.
    "/api/v1/analytics/quality": {"get"},
    "/api/v1/analytics/sources": {"get"},
    "/api/v1/analytics/trends": {"get"},
    # --- customers router (5) ---
    "/api/v1/customers": {"get", "post"},
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
    "/api/v1/tasks": {"get", "post"},
    "/api/v1/tasks/overdue/list": {"get"},
    "/api/v1/tasks/{task_id}": {"get", "put", "delete"},
    # --- usage (1) ---
    # Deliberate addition, not a modification: nothing above changed. Token spend
    # by scope, which no endpoint reported before -- the eight service-layer call
    # sites store no run, so the ledger is the only place their cost exists.
    "/api/v1/usage": {"get"},
    # --- diagnostics (1) ---
    # Read-only: circuit state and counters, run slots, pool occupancy. No
    # request body, no side effect, and nothing it returns had an endpoint.
    "/api/v1/diagnostics": {"get"},
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


def test_the_collection_urls_the_client_uses_answer_directly():
    """Not 307. A redirect here is not a wasted round trip; it is a broken page.

    Measured in a real browser: ``GET /api/v1/customers`` (what the axios
    instance builds from ``baseURL='/api/v1'`` plus ``/customers``) was a 307 to
    ``/api/v1/customers/``. Behind the Vite proxy the redirect target is written
    as an absolute ``http://localhost:8000/...`` because ``changeOrigin``
    rewrites Host; the browser follows it off the proxy's origin, gets no CORS
    headers from the API, and the customers and tasks pages render empty while
    every other page works. The cause is an URL shape, the symptom is an auth
    error, and no unit test could see it because every view is mocked.
    """
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        for url in ("/api/v1/customers", "/api/v1/tasks"):
            r = c.get(url, follow_redirects=False)
            assert r.status_code == 200, (
                f"{url} answered {r.status_code} "
                f"(location: {r.headers.get('location')!r}) instead of 200 -- "
                "the client's own URL must be the canonical one, not a redirect"
            )


def test_no_route_is_registered_with_a_trailing_slash():
    """The shape rule behind the test above, applied to routes added later.

    A collection root registered at ``"/"`` reads like a detail -- REST style
    even suggests it -- and quietly turns every call into a redirect. The rule
    is one line, so a future route does not have to re-discover the browser
    behaviour that made this fail.
    """
    live = _live_routes()
    offenders = sorted(p for p in live if p != "/" and p.endswith("/"))
    assert not offenders, (
        f"routes registered with a trailing slash: {offenders}\n"
        "register the collection root as \"\" so the URL a client builds from "
        "`baseURL + '/customers'` answers directly; `/customers/` still works "
        "through a redirect, but that redirect escapes the dev proxy"
    )


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


def test_the_typed_surface_matches_the_snapshot():
    """The route table is not the contract; the shapes are.

    ``test_route_count_unchanged`` catches a deleted route. It cannot see a new
    required field, a renamed one, a type widened from ``int`` to ``str``, a
    constraint, or a status code changed from 201 to 200 -- all of which break a
    client while leaving the route table identical. ``api_surface.json`` is the
    normalised OpenAPI surface (parameters, required fields, types, constraints,
    status codes), and this is the assertion that it did not move.

    When this fails, do not just re-run ``--update``. The question is whether a
    current client can break: if yes, the change needs a new path version or a
    deprecation window, not a new snapshot.
    """
    import json

    import api_surface

    previous = json.loads(api_surface.SNAPSHOT.read_text(encoding="utf-8"))
    current = api_surface.build_surface()
    changes = api_surface.diff(previous, current)

    assert not changes, (
        "the HTTP contract changed. Decide whether it is additive (then accept "
        "the snapshot) or breaking (then add /api/v2 or a deprecation window):\n"
        "  - " + "\n  - ".join(changes[:40])
    )


def test_the_surface_diff_names_the_field_that_changed():
    """The guard is only useful if its failure says *what* broke."""
    import api_surface

    before = {
        "POST /x": {"request": {"schema": {"properties": {"email": {"maxLength": 20000}}}}}
    }
    after = {
        "POST /x": {"request": {"schema": {"properties": {"email": {"maxLength": 40000}}}}}
    }

    assert api_surface.diff(before, after) == [
        "POST /x.request.schema.properties.email.maxLength: 20000 -> 40000"
    ]


def test_prose_does_not_change_the_surface():
    """A docstring edit must not fail the contract -- or people delete the test.

    Only types, names, required-ness, constraints and status codes are kept.
    Descriptions, summaries, examples and model titles are documentation.
    """
    import api_surface

    old = api_surface._resolve(
        {"type": "string", "title": "Email", "description": "old wording"}, {}
    )
    new = api_surface._resolve(
        {"type": "string", "title": "Email address", "description": "new wording"}, {}
    )

    assert old == new == {"type": "string"}
