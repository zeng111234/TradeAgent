"""Readiness is not liveness.

``/health`` answers for the process. A process whose database is gone keeps
answering it, while every request that touches data fails. These two tests pin
the difference: the probe asks the database a question, and it fails closed.
"""
from __future__ import annotations


def test_ready_answers_when_the_database_answers(client):
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


def test_ready_fails_closed_and_says_why(client, monkeypatch):
    import app.database as database

    class Unreachable:
        async def __aenter__(self):
            raise OSError("disk is gone")

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(database, "async_session", lambda: Unreachable())

    response = client.get("/ready")

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "database_unavailable"
    assert body["retryable"] is True
    assert "OSError" in body["detail"]
    assert body["request_id"]


def test_health_still_does_not_touch_the_database(client, monkeypatch):
    """The old probe must not start failing for a reason it never checked.

    If /health ever grows a database check, the two probes become the same probe
    and a restart loop becomes the response to a database outage.
    """
    import app.database as database

    class Unreachable:
        async def __aenter__(self):
            raise OSError("disk is gone")

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(database, "async_session", lambda: Unreachable())

    assert client.get("/health").status_code == 200
