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


def test_customer_contacts_and_notes_of_seeded_customer(client):
    """ABC Fashion Imports has 1 contact + 2 notes in the demo seed."""
    items = client.get("/api/v1/customers/").json()["items"]
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
