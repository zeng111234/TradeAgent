"""Endpoint-level characterisation tests (the refactor safety net).

Only read-only and no-LLM endpoints are exercised. Anything that would send
mail, hit SMTP, or call a model is deliberately excluded from the *callable*
set — those are covered by the route-contract snapshot instead.

The lifespan seeds five demo customers, three contacts, three notes,
two templates and three tasks (see ``main._insert_demo_data``). Tests assert
on shape and on that seeded reality, not on exact numbers that a future
schema change might legitimately move.
"""
from __future__ import annotations

import pytest


# --------------------------------------------------------------------------
# health / root
# --------------------------------------------------------------------------

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


# --------------------------------------------------------------------------
# customers
# --------------------------------------------------------------------------

def test_customer_list_is_paginated_envelope(client):
    r = client.get("/api/v1/customers/")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"items", "total", "page", "page_size", "pages"}
    assert body["total"] >= 5          # demo seed
    assert body["page"] == 1
    assert isinstance(body["items"], list)
    assert len(body["items"]) == min(body["page_size"], body["total"])


def test_customer_list_item_shape(client):
    items = client.get("/api/v1/customers/").json()["items"]
    assert items, "demo seed should provide customers"
    item = items[0]
    for key in ("id", "company_name", "country", "stage", "score", "created_at"):
        assert key in item, f"customer list item lost key: {key}"
    assert "contacts" in item
    assert "notes_list" in item


def test_customer_detail_roundtrip(client):
    first = client.get("/api/v1/customers/").json()["items"][0]
    r = client.get(f"/api/v1/customers/{first['id']}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["id"] == first["id"]
    assert detail["company_name"] == first["company_name"]
    assert isinstance(detail["contacts"], list)
    assert isinstance(detail["notes_list"], list)


def test_customer_detail_404_for_missing(client):
    assert client.get("/api/v1/customers/999999").status_code == 404


def test_customer_create_requires_company_name(client):
    """company_name is mandatory — 422 must be raised, not a 500."""
    r = client.post("/api/v1/customers/", json={"country": "Germany"})
    assert r.status_code == 422


def test_customer_create_returns_the_full_customer(client):
    """A 201, not a 500 raised while serialising the 201.

    ``contacts`` and ``notes_list`` are lazy relationships that
    ``CustomerResponse`` reads. Serialisation happens outside the async
    session's greenlet context, so a customer returned straight from ``flush``
    raised ``MissingGreenlet`` there -- the row was created and the caller was
    told the request failed. Nothing exercised the success path before.
    """
    r = client.post("/api/v1/customers/", json={"company_name": "Serialisation Test Co"})

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["company_name"] == "Serialisation Test Co"
    assert body["contacts"] == []
    assert body["notes_list"] == []
    assert isinstance(body["id"], int)


def test_customer_contacts_and_notes_of_seeded_customer(client):
    """ABC Fashion Imports has 1 contact + 2 notes in the demo seed.

    Looked up by name rather than by position. Other test files create
    customers, and the list is paginated: "the fifth seed customer" is only on
    page one until enough other tests have run, which makes this test pass or
    fail according to file order.
    """
    items = client.get(
        "/api/v1/customers/", params={"search": "ABC Fashion Imports"}
    ).json()["items"]
    abc = next((c for c in items if c["company_name"] == "ABC Fashion Imports LLC"), None)
    assert abc is not None, "demo seed changed; update this test's anchor customer"

    contacts = client.get(f"/api/v1/customers/{abc['id']}/contacts")
    assert contacts.status_code == 200
    assert isinstance(contacts.json(), list)
    assert len(contacts.json()) >= 1

    notes = client.get(f"/api/v1/customers/{abc['id']}/notes")
    assert notes.status_code == 200
    assert isinstance(notes.json(), list)
    assert len(notes.json()) >= 1


# --------------------------------------------------------------------------
# analytics — five read-only aggregates over the seed
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/analytics/dashboard",
        "/api/v1/analytics/pipeline",
        "/api/v1/analytics/sources",
        "/api/v1/analytics/countries",
        "/api/v1/analytics/trends",
    ],
)
def test_analytics_endpoints_return_json(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"
    assert r.json() is not None


def test_analytics_dashboard_shape(client):
    body = client.get("/api/v1/analytics/dashboard").json()
    assert isinstance(body, dict) and body, "dashboard must not be empty"


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------

def test_task_list_and_overdue(client):
    r = client.get("/api/v1/tasks/")
    assert r.status_code == 200
    assert isinstance(r.json(), list)

    r = client.get("/api/v1/tasks/overdue/list")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# --------------------------------------------------------------------------
# emails (read-only parts only)
# --------------------------------------------------------------------------

def test_email_templates_listed(client):
    r = client.get("/api/v1/emails/templates")
    assert r.status_code == 200
    templates = r.json()
    assert isinstance(templates, list)
    assert len(templates) >= 2, "demo seed defines two templates"
    assert {"id", "name", "subject", "body"} <= set(templates[0])


def test_email_logs_and_stats(client):
    r = client.get("/api/v1/emails/logs")
    assert r.status_code == 200
    assert isinstance(r.json(), list)

    r = client.get("/api/v1/emails/stats")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


# --------------------------------------------------------------------------
# scheduler introspection
# --------------------------------------------------------------------------

def test_scheduler_jobs_listed(client):
    r = client.get("/api/v1/scheduler/jobs")
    assert r.status_code == 200
    body = r.json()
    assert "jobs" in body
    assert isinstance(body["jobs"], list)


# --------------------------------------------------------------------------
# agent endpoints that work without an LLM key
# --------------------------------------------------------------------------

def test_agent_reply_analysis_endpoint(client):
    r = client.post("/api/v1/agent/analyze-email", json={"email_content": "what is your price?"})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "price_negotiation"
    assert body["ai_powered"] is False


def test_agent_inquiry_analysis_endpoint(client):
    r = client.post("/api/v1/agent/analyze-inquiry", json={"email_content": "please quote FOB"})
    assert r.status_code == 200
    body = r.json()
    assert body["inquiry_type"] == "price_request"
    assert body["delivery_requirements"]["shipping_terms"] == "FOB"


def test_agent_holidays_endpoint(client):
    r = client.get("/api/v1/agent/holidays")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_agent_endpoints_validate_their_bodies(client):
    """Missing required fields must be 422, never a 500."""
    assert client.post("/api/v1/agent/analyze-email", json={}).status_code == 422
    assert client.post("/api/v1/agent/analyze-inquiry", json={}).status_code == 422
    assert client.post("/api/v1/agent/negotiation-advice", json={}).status_code == 422
    assert client.post("/api/v1/agent/analyze-website", json={}).status_code == 422


def test_an_email_too_large_to_analyse_in_one_call_is_refused(client):
    """The prompt is a concatenation; an unbounded field is an unbounded bill.

    One call, max_tokens=1000, an 80-second logical budget. A two-megabyte paste
    is a mailbox export, not an email -- and truncating it silently would produce
    a confident analysis of a conversation whose ending the model never saw.
    """
    r = client.post(
        "/api/v1/agent/analyze-email",
        json={"email_content": "x" * 20_001},
    )

    assert r.status_code == 422
    assert "20000" in r.text


# --------------------------------------------------------------------------
# Error messages answer the reader's next question
# --------------------------------------------------------------------------
# The convention across this codebase: say what did NOT happen (safe to
# retry? half-written data?), give the typed cause rather than a raw trace
# string, and route the reader (a 500 sends an operator hunting for a bug, so
# the text must say whether any bug is involved). These pin the two 500s that
# used to leak `str(e)` and answer neither question.


def test_an_unreadable_leads_file_names_the_path_and_the_cause(client, monkeypatch):
    """The file exists and still could not be read -- so the typed cause is
    the diagnosis, and the path tells the reader which file to look at."""
    import app.routers.agent as agent_routes

    def explode(_path):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(agent_routes, "_read_leads_file", explode)

    r = client.get("/api/v1/agent/import-leads/preview")
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "ValueError" in detail, "the typed cause is the diagnosis"
    assert "could not be read" in detail
    assert "daily_leads.json" in detail, "name the file, so the reader knows which one"


def test_a_failed_import_says_it_was_rolled_back(client, monkeypatch):
    """The reader's question after a failed import is "is there half-written
    data in my book?" -- the message must answer it."""
    import app.routers.agent as agent_routes

    def fake_read(_path):
        return {
            "leads": [
                {
                    "company_name": "Broken Score Ltd",
                    "website": "https://broken-score.example.test",
                    "relevance_score": "not-a-number",
                    "emails": ["buyer@broken-score.example.test"],
                }
            ]
        }

    monkeypatch.setattr(agent_routes, "_read_leads_file", fake_read)

    r = client.post("/api/v1/agent/import-leads")
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "rolled back" in detail
    assert "no customers were written" in detail, (
        "the negative space is the part the reader is waiting for"
    )
    assert "ValueError" in detail
