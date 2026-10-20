"""The error contract: status, code, detail, request_id.

What these tests are for
------------------------
A status code says *how* the request failed; it cannot say *what* condition was
broken. ``404`` is both "no customer 42" and "you spelled the route wrong", and
a client that only reads the status treats them the same -- one is a bug in the
caller's code, the other is a normal answer. ``code`` is the part a client may
branch on, so it has its own rules: stable, specific, free of ids, never
reworded. These tests pin those rules rather than the wording of any message.

``detail`` stays where v1 put it, with the shape v1 gave it (a string, except
for 422 where it is a list). Moving it into an ``error`` object would be a
nicer envelope and a breaking change for every caller that already reads
``detail`` -- and the rule from R49 is that a breaking change gets a new path,
not a nicer shape in the old one.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.errors import FALLBACK_CODES, ErrorCode
from app.main import app

APP_DIR = Path(__file__).resolve().parent.parent / "app"


# --------------------------------------------------------------------------
# The code table itself
# --------------------------------------------------------------------------


def test_a_code_is_a_category_not_a_sentence():
    """``customer_not_found``, never ``customer_42_not_found``.

    An id in a code means every id is a new code; a client cannot branch on a
    set that grows with the data, and the table stops being reviewable.
    """
    for code in ErrorCode:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", code.value), code
        assert not any(ch.isdigit() for ch in code.value), code


def test_no_two_codes_share_a_value():
    values = [code.value for code in ErrorCode]
    assert len(values) == len(set(values))


def test_the_fallback_table_only_names_codes_that_exist():
    """A typo here would surface as an ``AttributeError`` on a 404."""
    for status, code in FALLBACK_CODES.items():
        assert isinstance(code, ErrorCode), (status, code)


def test_every_error_is_raised_with_a_code():
    """A bare ``HTTPException`` is how the contract leaks.

    It would still get a fallback code (``not_found``), but the caller would
    learn nothing beyond the status -- which is exactly what the code is for.
    Enforced statically so a new route cannot quietly opt out.
    """
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            name = getattr(node.exc.func, "id", "")
            if name == "HTTPException":
                offenders.append(f"{path.name}:{node.lineno} raise HTTPException")
            elif name == "ApiError":
                if not any(k.arg == "code" for k in node.exc.keywords):
                    offenders.append(f"{path.name}:{node.lineno} ApiError without code=")
    assert not offenders, offenders


def test_every_referenced_code_exists():
    """``ErrorCode.EMAIL_SEND_FAILED`` was written before it was declared.

    The enum makes the value a string, so a missing member is not a typo the
    type checker catches at import -- it is an ``AttributeError`` on the error
    path, which is the one place that must not fail. Checked statically.
    """
    declared = {code.name for code in ErrorCode}
    missing = []
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ErrorCode"
                and node.attr not in declared
            ):
                missing.append(f"{path.name}:{node.lineno} ErrorCode.{node.attr}")
    assert not missing, missing


def test_a_code_never_maps_to_two_status_codes():
    """Otherwise branching on the code still requires checking the status.

    Found by this test, not by review: ``email_not_a_draft`` was a 400 on the
    send path (v1 shipped it that way) and a 409 on edit/delete. The statuses
    stayed -- changing one would be a breaking change -- and the send path got
    its own code.
    """
    seen: dict[str, tuple[int, str]] = {}
    conflicts = []
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            if getattr(node.exc.func, "id", "") != "ApiError":
                continue
            kwargs = {k.arg: k.value for k in node.exc.keywords}
            if "code" not in kwargs or "status_code" not in kwargs:
                continue
            code = ast.unparse(kwargs["code"])
            status = ast.literal_eval(kwargs["status_code"])
            where = f"{path.name}:{node.lineno}"
            if code in seen and seen[code][0] != status:
                conflicts.append(f"{code}: {seen[code][0]} at {seen[code][1]}, {status} at {where}")
            else:
                seen.setdefault(code, (status, where))
    assert not conflicts, conflicts


# --------------------------------------------------------------------------
# The response
# --------------------------------------------------------------------------


def test_a_missing_customer_names_the_resource(client):
    response = client.get("/api/v1/customers/987654")
    assert response.status_code == 404

    body = response.json()
    assert body["code"] == "customer_not_found"
    assert body["retryable"] is False
    assert body["request_id"] == response.headers["X-Request-Id"]
    assert isinstance(body["detail"], str)


def test_two_404s_are_told_apart_by_their_code(client):
    """The whole reason ``code`` exists.

    A client can act on ``customer_not_found`` (show an empty state) and must
    not act the same way on ``not_found`` (the route is wrong: fix the caller).
    """
    route = client.get("/api/v1/no-such-route").json()
    customer = client.get("/api/v1/customers/987654").json()

    assert route["code"] == "not_found"
    assert customer["code"] == "customer_not_found"


def test_the_wrong_method_gets_a_code_too(client):
    response = client.delete("/api/v1/customers/")
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"


def test_a_validation_error_says_which_field_and_not_what_was_in_it(client):
    """Pydantic echoes the offending input; the response must not.

    A 20 000-character email came back verbatim inside the 422 -- the error was
    as large as the request, and the customer's text ended up in browser
    consoles and error trackers. ``loc`` and ``msg`` are enough to fix the call.
    """
    secret = "SECRET-MARKER-" + "x" * 20_001
    response = client.post("/api/v1/agent/analyze-email", json={"email_content": secret})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["retryable"] is False
    assert body["detail"][0]["loc"] == ["body", "email_content"]
    assert "input" not in body["detail"][0]
    assert "SECRET-MARKER" not in response.text


def test_a_missing_field_is_a_list_not_a_string(client):
    """v1 returned a list for 422 and a string elsewhere; both are load-bearing."""
    body = client.post("/api/v1/agent/analyze-email", json={}).json()
    assert isinstance(body["detail"], list)
    assert body["detail"][0]["loc"] == ["body", "email_content"]


def test_a_conflict_says_which_rule_refused_it(client):
    listed = client.get("/api/v1/customers", params={"page_size": 1}).json()
    customer = listed["items"][0]

    sent = client.post(
        "/api/v1/emails/send",
        json={
            "customer_id": customer["id"],
            "to_email": "buyer@example.test",
            "to_name": "Buyer",
            "subject": "error contract probe",
            "body": "<p>probe</p>",
        },
    )
    assert sent.status_code == 200, sent.text

    response = client.put(
        f"/api/v1/emails/logs/{sent.json()['id']}",
        json={"subject": "edited", "body": "<p>edited</p>"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "email_not_a_draft"


def test_capacity_says_when_to_come_back(client, monkeypatch):
    """429 with no ``Retry-After`` invites a retry storm.

    The server knows a run is bounded, so "later" is a number, not a guess.
    """
    from app.routers import agent_runs

    monkeypatch.setattr(agent_runs, "is_configured", lambda: True)
    monkeypatch.setattr(agent_runs, "_in_flight", {"a", "b", "c"})

    response = client.post("/api/v1/agent/run", json={"goal": "probe", "wait": True})

    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "agent_at_capacity"
    assert body["retryable"] is True
    assert body["details"] == {"in_flight": 3, "limit": agent_runs.MAX_CONCURRENT_RUNS}
    assert response.headers["Retry-After"] == "5"


def test_retryable_is_not_a_mirror_of_the_status(client, monkeypatch):
    """``400 + retryable: true`` is legal, and here it is correct.

    Reaching a customer's website is not part of the request's shape: the URL
    was well formed and the site did not answer. v1 returns 400 for it and that
    status cannot change without a new version, so the field that says "try
    again" has to be able to disagree with the status.
    """
    from app.routers import agent as agent_router

    async def unreachable(url, products=""):
        return {"error": "Failed to fetch website: connection refused", "score": None}

    monkeypatch.setattr(agent_router, "analyze_customer_website", unreachable)

    response = client.post(
        "/api/v1/agent/analyze-website", json={"url": "http://example.test"}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "website_unreachable"
    assert response.json()["retryable"] is True


def test_an_unhandled_error_is_json_and_keeps_the_secret_out(monkeypatch):
    """A 500 used to be Starlette's plain-text body: unparseable, no id.

    The exception text is the one thing that must not travel: it carries
    connection strings and library internals, and the caller can act on none
    of it.
    """
    from app.services import customer_service

    async def boom(*args, **kwargs):
        raise RuntimeError("postgres://user:hunter2@db.internal/trade")

    monkeypatch.setattr(customer_service, "list_customers", boom)

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/api/v1/customers")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["retryable"] is False
    assert "hunter2" not in response.text
    assert body["request_id"] == response.headers["X-Request-Id"]


# --------------------------------------------------------------------------
# The request id
# --------------------------------------------------------------------------


def test_a_safe_caller_supplied_id_is_echoed(client):
    response = client.get(
        "/api/v1/customers", headers={"X-Request-Id": "checkout-7f3a-11"}
    )
    assert response.headers["X-Request-Id"] == "checkout-7f3a-11"


@pytest.mark.parametrize("raw", ["short", "has space", "x" * 65, "bad:value"])
def test_an_unusable_id_is_replaced_not_mangled(client, raw):
    """A mangled id is worse than a new one: it looks correlated and is not."""
    response = client.get("/api/v1/customers", headers={"X-Request-Id": raw})
    returned = response.headers["X-Request-Id"]
    assert returned != raw
    assert len(returned) == 16
    assert re.fullmatch(r"[0-9a-f]{16}", returned)


def test_every_response_has_an_id_not_just_errors(client):
    """Without it on the success path, a 200 that behaved oddly has no handle."""
    response = client.get("/api/v1/customers")
    assert len(response.headers["X-Request-Id"]) == 16
