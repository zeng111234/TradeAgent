"""Create endpoints: the same request twice produces one row.

The failure this prevents needs no malice. A client posts a customer, the
connection drops after the server commits, and the client retries because it
has no other way to find out. Without a key it creates a second customer, and
nothing in the response says so.

Each test uses a unique body so it cannot collide with rows left by another
file in the shared test database.
"""
from __future__ import annotations

from uuid import uuid4

HEADER = "Idempotency-Key"


def _customer_body(name: str) -> dict:
    # ``new`` is deliberate: it is outside the follow-up plan's stages, so a
    # leftover row cannot displace another test's customer from that top-N list.
    return {"company_name": name, "country": "DE", "stage": "new"}


def test_the_same_key_and_body_create_one_customer(client):
    name = f"Idem Customer {uuid4().hex[:8]}"
    key = uuid4().hex

    first = client.post("/api/v1/customers/", json=_customer_body(name),
                        headers={HEADER: key})
    second = client.post("/api/v1/customers/", json=_customer_body(name),
                         headers={HEADER: key})

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    assert "Idempotent-Replay" not in first.headers
    assert second.headers.get("Idempotent-Replay") == "true"

    listing = client.get("/api/v1/customers/", params={"search": name}).json()
    assert listing["total"] == 1, listing

    client.delete(f"/api/v1/customers/{first.json()['id']}")


def test_the_same_key_with_a_different_body_is_a_conflict(client):
    key = uuid4().hex
    first = client.post("/api/v1/customers/",
                        json=_customer_body(f"Idem A {uuid4().hex[:6]}"),
                        headers={HEADER: key})
    second = client.post("/api/v1/customers/",
                         json=_customer_body(f"Idem B {uuid4().hex[:6]}"),
                         headers={HEADER: key})

    assert first.status_code == 201
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "idempotency_key_reuse"

    client.delete(f"/api/v1/customers/{first.json()['id']}")


def test_without_a_key_the_endpoint_behaves_as_before(client):
    name = f"Idem NoKey {uuid4().hex[:8]}"

    first = client.post("/api/v1/customers/", json=_customer_body(name))
    second = client.post("/api/v1/customers/", json=_customer_body(name))

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]

    client.delete(f"/api/v1/customers/{first.json()['id']}")
    client.delete(f"/api/v1/customers/{second.json()['id']}")


def test_an_empty_key_is_refused_rather_than_ignored(client):
    response = client.post("/api/v1/customers/",
                           json=_customer_body(f"Idem Empty {uuid4().hex[:6]}"),
                           headers={HEADER: "   "})

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"


def test_a_contact_is_retry_safe(client):
    customer = client.post("/api/v1/customers/",
                           json=_customer_body(f"Idem Contact {uuid4().hex[:6]}")).json()
    key = uuid4().hex
    body = {"name": "Buyer", "email": f"{uuid4().hex[:6]}@example.test"}

    first = client.post(f"/api/v1/customers/{customer['id']}/contacts",
                        json=body, headers={HEADER: key})
    second = client.post(f"/api/v1/customers/{customer['id']}/contacts",
                         json=body, headers={HEADER: key})

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.headers.get("Idempotent-Replay") == "true"

    client.delete(f"/api/v1/customers/{customer['id']}")


def test_a_note_is_retry_safe(client):
    customer = client.post("/api/v1/customers/",
                           json=_customer_body(f"Idem Note {uuid4().hex[:6]}")).json()
    key = uuid4().hex
    body = {"content": "Called, asked for samples", "note_type": "call"}

    first = client.post(f"/api/v1/customers/{customer['id']}/notes",
                        json=body, headers={HEADER: key})
    second = client.post(f"/api/v1/customers/{customer['id']}/notes",
                         json=body, headers={HEADER: key})

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    client.delete(f"/api/v1/customers/{customer['id']}")


def test_a_task_is_retry_safe(client):
    key = uuid4().hex
    body = {"title": f"Idem Task {uuid4().hex[:8]}", "priority": "medium"}

    first = client.post("/api/v1/tasks/", json=body, headers={HEADER: key})
    second = client.post("/api/v1/tasks/", json=body, headers={HEADER: key})

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.headers.get("Idempotent-Replay") == "true"


def test_a_template_is_retry_safe(client):
    key = uuid4().hex
    body = {"name": f"Idem Template {uuid4().hex[:8]}", "subject": "Hi",
            "body": "<p>Hello</p>"}

    first = client.post("/api/v1/emails/templates", json=body, headers={HEADER: key})
    second = client.post("/api/v1/emails/templates", json=body, headers={HEADER: key})

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.headers.get("Idempotent-Replay") == "true"
