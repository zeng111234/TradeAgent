"""Recording a reply, over HTTP, and what it changes downstream.

The pure rules are in ``test_thread_state.py``. This file is about whether the
state can be *entered at all* from the product, and whether entering it changes
what the app tells a human.

Before this endpoint there was no way to record a reply: no route, no workflow
and no agent tool ever set ``replied_at`` or moved a row to ``REPLIED``.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

# Demo seed (main._insert_demo_data): 4 is Japan Embroidery, stage 'contacted',
# which is the stage the follow-up list reasons about.
CUSTOMER_ID = 4
TO_EMAIL = "yuki@japanembroidery.co.jp"


def send(client, subject: str):
    """Send one email through the real endpoint (SMTP is simulated in tests)."""
    response = client.post(
        "/api/v1/emails/send",
        json={
            "customer_id": CUSTOMER_ID,
            "to_email": TO_EMAIL,
            "to_name": "Yuki Tanaka",
            "subject": subject,
            "body": f"<p>{subject}</p>",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def follow_up_action(client):
    """The follow-up entry for our customer, or None."""
    response = client.post("/api/v1/agent/daily-intelligence")
    assert response.status_code == 200, response.text
    for action in response.json().get("actions", []):
        if action.get("customer_id") == CUSTOMER_ID:
            return action
    return None


# --------------------------------------------------------------------------
# The scenario
# --------------------------------------------------------------------------

def test_a_reply_to_the_third_email_is_representable_and_means_we_owe_one(client):
    first = send(client, "Japan Embroidery - gold thread")
    second = send(client, "Following up - Japan Embroidery")
    third = send(client, "Second follow-up - Japan Embroidery")

    reply = client.post(f"/api/v1/emails/logs/{third['id']}/reply")
    assert reply.status_code == 200, reply.text
    body = reply.json()

    assert body["recorded"] is True
    assert body["status"] == "replied"
    assert body["already_recorded"] is False

    conversation = body["conversation"]
    assert conversation["outbound"] == 3
    assert conversation["answered"] is True
    assert conversation["superseded"] == 2, (
        "the first two emails were not ignored; the reply overtook them"
    )
    assert conversation["awaiting_reply"] == 0
    assert conversation["they_wrote_last"] is True, "we owe them an answer"
    # And the two that came before are still recorded as delivered, not deleted.
    logs = client.get("/api/v1/emails/logs").json()
    by_id = {row["id"]: row for row in logs}
    assert by_id[first["id"]]["status"] == "sent"
    assert by_id[second["id"]]["status"] == "sent"
    assert by_id[third["id"]]["status"] == "replied"
    assert by_id[third["id"]]["replied_at"] is not None


def test_the_follow_up_list_stops_treating_an_answered_customer_as_silent(client):
    """The behaviour a human actually sees, before and after the reply."""
    send(client, "Japan Embroidery - gold thread")
    send(client, "Following up - Japan Embroidery")
    third = send(client, "Second follow-up - Japan Embroidery")

    before = follow_up_action(client)
    assert before is not None
    assert "Replied" not in before["reason"], before
    assert before["suggested_action"] != "Answer their message - they wrote last"

    assert client.post(f"/api/v1/emails/logs/{third['id']}/reply").status_code == 200

    after = follow_up_action(client)
    assert after is not None
    assert "have not heard back" in after["reason"], after
    assert after["suggested_action"] == "Answer their message - they wrote last"
    assert after["priority"] == "high"
    # The stage is left alone: a reply is contact, not a stage transition.
    assert after["stage"] == "contacted"


def test_recording_the_same_reply_twice_does_not_move_the_timestamp(client):
    third = send(client, "Second follow-up - Japan Embroidery")

    first = client.post(f"/api/v1/emails/logs/{third['id']}/reply").json()
    second = client.post(f"/api/v1/emails/logs/{third['id']}/reply").json()

    assert first["recorded"] is True
    assert second["recorded"] is False
    assert second["already_recorded"] is True
    assert second["replied_at"] == first["replied_at"], (
        "a reply happened once; a second call is a no-op"
    )


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

def test_an_email_that_was_never_delivered_cannot_have_been_answered(client, monkeypatch):
    async def always_fails(*args, **kwargs):
        return {"success": False, "error": "smtp refused"}

    monkeypatch.setattr("app.routers.emails.send_email", always_fails)
    failed = send(client, "This one never left")

    logs = {row["id"]: row for row in client.get("/api/v1/emails/logs").json()}
    assert logs[failed["id"]]["status"] == "failed"

    response = client.post(f"/api/v1/emails/logs/{failed['id']}/reply")
    assert response.status_code == 409
    assert "never delivered" in response.json()["detail"]


def test_a_reply_from_the_future_is_refused(client):
    sent = send(client, "Japan Embroidery - gold thread")
    future = (datetime.utcnow() + timedelta(days=1)).isoformat()

    response = client.post(
        f"/api/v1/emails/logs/{sent['id']}/reply", json={"replied_at": future}
    )
    assert response.status_code == 422
    assert "future" in response.json()["detail"]


def test_a_reply_cannot_predate_the_email_it_answers(client):
    """Storing it would make the whole derivation nonsense.

    Every message would appear to have been sent after the reply, so none would
    count as awaiting one and the conversation would read as settled.
    """
    sent = send(client, "Japan Embroidery - gold thread")
    earlier = (datetime.utcnow() - timedelta(days=3)).isoformat()

    response = client.post(
        f"/api/v1/emails/logs/{sent['id']}/reply", json={"replied_at": earlier}
    )
    assert response.status_code == 422
    assert "before this email was sent" in response.json()["detail"]


def test_replying_to_an_unknown_email_is_a_404(client):
    assert client.post("/api/v1/emails/logs/999999/reply").status_code == 404


def test_the_reply_endpoint_shows_up_in_the_capability_surface(client):
    """Not a contract test: a reminder that this is the only write path."""
    stats = client.get("/api/v1/emails/stats").json()
    assert "reply_rate" in stats

    sent = send(client, "Japan Embroidery - gold thread")
    client.post(f"/api/v1/emails/logs/{sent['id']}/reply")

    after = client.get("/api/v1/emails/stats").json()
    assert after["total_replied"] >= 1, (
        "reply_rate was 0.0% by construction before this endpoint existed"
    )
