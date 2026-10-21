"""The shared-password gate.

The gate is what makes a public deployment survivable: behind one link, every
agent run costs money and a configured mailbox sends real mail. Two properties
are the whole contract --

1. **Inert when unarmed.** With no password, nothing here runs. A local
   instance behaves exactly as it did before this module existed.
2. **Total when armed.** With a password, nothing is reachable without the
   cookie except what the gate itself needs to be opened.

The second is the one that fails quietly: a gate that misses one prefix is a
gate that looks like it works until somebody walks through the hole, so the
tests below go at it from several directions rather than checking one path.
"""
from __future__ import annotations

import pytest

from app import access
from app.config import settings

# Deliberately not the value any deployment uses. This file is public, and a
# test constant that happens to match the live gate hands the password to
# anyone who reads the repo -- which is what the previous constant did.
PASSWORD = "test-only-password-7f3a"


@pytest.fixture
def armed(monkeypatch):
    """Turn the gate on, with a clean throttle for each test."""
    monkeypatch.setattr(settings, "APP_PASSWORD", PASSWORD, raising=False)
    access.throttle.per_source.clear()
    access.throttle.global_failures.clear()
    yield
    access.throttle.per_source.clear()
    access.throttle.global_failures.clear()


def sign_in(client, password=PASSWORD, follow_redirects=False):
    return client.post(
        "/login",
        data={"password": password, "next": "/console"},
        follow_redirects=follow_redirects,
    )


# --------------------------------------------------------------------------
# Unarmed: nothing changes
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unarmed_the_api_answers_as_before(client):
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/customers").status_code == 200


@pytest.mark.asyncio
async def test_unarmed_sign_in_says_the_gate_is_not_on(client):
    """No password means no sign-in, and saying so beats redirecting
    somewhere the caller did not ask to go."""
    response = client.post("/login", data={"password": "anything"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unarmed_the_login_page_still_answers(client):
    assert client.get("/login").status_code == 200


# --------------------------------------------------------------------------
# Armed: nothing leaks
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_armed_an_api_call_is_refused_with_a_code(client, armed):
    response = client.get("/api/v1/customers")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"
    # HTML would have the caller render a login page as data.
    assert "text/html" not in response.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_armed_a_page_request_is_sent_to_the_login_page(client, armed):
    response = client.get("/console", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert "/login" in response.headers["location"]


@pytest.mark.asyncio
async def test_armed_the_docs_are_not_public(client, armed):
    """The API surface is not something to hand a stranger a map of."""
    assert client.get("/openapi.json").status_code == 401


@pytest.mark.asyncio
async def test_armed_the_login_page_is_reachable(client, armed):
    response = client.get("/login")
    assert response.status_code == 200
    assert "password" in response.text.lower()


@pytest.mark.asyncio
async def test_armed_the_probes_still_answer(client, armed):
    """/health and /ready are what a platform asks before sending traffic.
    Gating them would take the app offline by its own liveness check."""
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code in (200, 503)


# --------------------------------------------------------------------------
# Signing in
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_right_password_grants_access(client, armed):
    response = sign_in(client)
    assert response.status_code == 303
    assert access.COOKIE_NAME in response.cookies

    # The same client now carries the cookie on every request.
    assert client.get("/api/v1/customers").status_code == 200


@pytest.mark.asyncio
async def test_signing_in_returns_you_to_where_you_were_going(client, armed):
    """The page you asked for, not the front door.

    Asserted because it was silently dropped once: the redirect was checked
    for its status code and not its target, so a login that always landed on
    "/" passed every test in the file.
    """
    response = sign_in(client)
    assert response.headers["location"] == "/console"


@pytest.mark.asyncio
async def test_the_next_parameter_cannot_point_somewhere_else(client, armed):
    """An open redirect would make the login page a forwarding service for
    any address on the internet, carrying this app's URL as cover."""
    for hostile in ("https://evil.example/", "//evil.example/", "javascript:alert(1)"):
        response = client.post(
            "/login", data={"password": PASSWORD, "next": hostile}, follow_redirects=False
        )
        assert response.headers["location"] == "/", hostile


@pytest.mark.asyncio
async def test_the_wrong_password_grants_nothing(client, armed):
    response = sign_in(client, password="0000")
    assert response.status_code == 401
    assert access.COOKIE_NAME not in response.cookies
    assert client.get("/api/v1/customers").status_code == 401


@pytest.mark.asyncio
async def test_a_forged_cookie_is_refused(client, armed):
    """The payload is just a timestamp -- anyone can write one. What makes a
    cookie worth anything is that the signature is checked."""
    client.cookies.set(access.COOKIE_NAME, "9999999999.deadbeef")
    assert client.get("/api/v1/customers").status_code == 401


@pytest.mark.asyncio
async def test_an_expired_cookie_is_refused(client, armed):
    """A valid signature over an old timestamp is still an old timestamp."""
    long_ago = access.time.time() - 2 * access.SESSION_TTL_SECONDS
    client.cookies.set(access.COOKIE_NAME, access.issue_session(now=long_ago))
    assert client.get("/api/v1/customers").status_code == 401


@pytest.mark.asyncio
async def test_signing_out_takes_access_away(client, armed):
    sign_in(client)
    assert client.get("/api/v1/customers").status_code == 200
    client.post("/logout")
    assert client.get("/api/v1/customers").status_code == 401


# --------------------------------------------------------------------------
# Brute force
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repeated_failures_pause_the_login(client, armed):
    """A short password is enumerable in seconds, so the throttle is the part
    that has to hold -- not the part that is nice to have."""
    for _ in range(access.MAX_FAILURES_PER_SOURCE):
        assert sign_in(client, password="0000").status_code == 401
    assert sign_in(client, password="0000").status_code == 429


@pytest.mark.asyncio
async def test_a_correct_password_after_failures_still_works(client, armed):
    """The throttle counts failures, not attempts: locking out the person who
    knows the password would be the gate doing the attacker's job."""
    for _ in range(access.MAX_FAILURES_PER_SOURCE - 1):
        sign_in(client, password="0000")
    assert sign_in(client).status_code == 303


# --------------------------------------------------------------------------
# The cookie itself
# --------------------------------------------------------------------------

def test_a_session_survives_a_round_trip_and_then_expires():
    value = access.issue_session()
    assert access.session_is_valid(value)
    future = access.time.time() + access.SESSION_TTL_SECONDS + 60
    assert not access.session_is_valid(value, now=future)


def test_garbage_is_not_a_session():
    for value in (None, "", "abc", "123", "123.", ".abc", "123.abc.def"):
        assert not access.session_is_valid(value)
