"""A mail can say which suggestion produced it, and the record says it once.

Why this file exists
--------------------
The plan computes a kind ("chase them again") and an attempt ("this is the
second try") for every customer, and used to throw both away the moment the
draft existed. A reply was therefore recorded against a customer but not
against the advice, so "which suggestion gets answered" was unanswerable -- and
the quality report said so in words instead of inventing a number.

Three properties are asserted here, because each is a way the data could look
present while being wrong:

* the value is **stored**, not only echoed back in the response;
* an unrecognised kind is **refused** rather than bucketed -- three synonyms
  would be three buckets with one row each;
* a replay does **not relabel** a mail that was already sent. Provenance
  describes the first send; a retry that disagrees is a caller bug, not a
  correction to history.
"""
from __future__ import annotations

import uuid

from sqlalchemy import delete, select

from app.models.customer import Customer, CustomerContact
from app.models.email_log import EmailLog

from app.routers import emails as emails_router


async def _a_customer(db) -> tuple[int, str]:
    """A throwaway customer in a closed stage, so the planner never sees it."""
    suffix = uuid.uuid4().hex[:8]
    customer = Customer(
        company_name=f"Provenance {suffix}",
        country="Testland",
        industry="testing",
        stage="completed",
        source="manual",
    )
    db.add(customer)
    await db.commit()
    await db.refresh(customer)

    email = f"provenance-{suffix}@example.test"
    db.add(CustomerContact(customer_id=customer.id, name="Buyer", email=email, is_primary=1))
    await db.commit()
    return customer.id, email


async def _cleanup(db, customer_id: int) -> None:
    await db.execute(delete(EmailLog).where(EmailLog.customer_id == customer_id))
    await db.execute(delete(CustomerContact).where(CustomerContact.customer_id == customer_id))
    await db.execute(delete(Customer).where(Customer.id == customer_id))
    await db.commit()


def _stub_send(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_send(**kwargs):
        calls.append(kwargs)
        return {"success": True, "error": None}

    monkeypatch.setattr(emails_router, "send_email", fake_send)
    return calls


def _message(customer_id: int, to_email: str, **extra) -> dict:
    return {
        "customer_id": customer_id,
        "to_email": to_email,
        "to_name": "Buyer",
        "subject": "Following up on the quote",
        "body": "<p>Just checking in.</p>",
        **extra,
    }


async def test_the_suggestion_is_stored_next_to_the_mail(client, db, monkeypatch):
    """Echoing the kind back is not enough; the row has to carry it."""
    _stub_send(monkeypatch)
    customer_id, email = await _a_customer(db)
    try:
        response = client.post(
            "/api/v1/emails/send",
            json=_message(customer_id, email,
                          suggestion_kind="send_follow_up",
                          suggestion_attempt=1),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["suggestion_kind"] == "send_follow_up"
        assert body["suggestion_attempt"] == 1

        db.expire_all()
        row = (await db.execute(
            select(EmailLog).where(EmailLog.id == body["id"])
        )).scalar_one()
        assert row.suggestion_kind == "send_follow_up"
        assert row.suggestion_attempt == 1
    finally:
        await _cleanup(db, customer_id)


async def test_a_kind_the_plan_could_not_have_produced_is_refused(client, db, monkeypatch):
    """A typo must fail loudly; a silently stored synonym is a bucket with no peer."""
    calls = _stub_send(monkeypatch)
    customer_id, email = await _a_customer(db)
    try:
        response = client.post(
            "/api/v1/emails/send",
            json=_message(customer_id, email, suggestion_kind="send_followup"),
        )
        assert response.status_code == 422, response.text
        detail = str(response.json()["detail"])
        assert "unknown suggestion_kind" in detail
        # The message names the vocabulary, because the caller is usually a UI
        # forwarding what the plan gave it and the fix is at the wiring.
        assert "send_follow_up" in detail
        assert calls == [], "the mail was handed to SMTP despite an invalid kind"
    finally:
        await _cleanup(db, customer_id)


async def test_an_attempt_without_a_kind_is_refused(client, db, monkeypatch):
    """'This was the second try' attributes a mail to nothing."""
    _stub_send(monkeypatch)
    customer_id, email = await _a_customer(db)
    try:
        response = client.post(
            "/api/v1/emails/send",
            json=_message(customer_id, email, suggestion_attempt=2),
        )
        assert response.status_code == 422, response.text
        assert "requires suggestion_kind" in str(response.json()["detail"])
    finally:
        await _cleanup(db, customer_id)


async def test_a_known_kind_without_a_count_is_kept_as_known(client, db, monkeypatch):
    """Not every caller knows how many tries preceded this one.

    The alternative -- defaulting to 0 -- would record first contacts that never
    happened, which is the failure this column exists to avoid. NULL is reported
    as ``unknown`` by the quality report.
    """
    _stub_send(monkeypatch)
    customer_id, email = await _a_customer(db)
    try:
        response = client.post(
            "/api/v1/emails/send",
            json=_message(customer_id, email, suggestion_kind="answer_them"),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["suggestion_kind"] == "answer_them"
        assert body["suggestion_attempt"] is None
    finally:
        await _cleanup(db, customer_id)


async def test_a_replay_does_not_relabel_a_mail_that_was_sent(client, db, monkeypatch):
    """The same key may only ever describe one send, including what it answered."""
    calls = _stub_send(monkeypatch)
    customer_id, email = await _a_customer(db)
    key = {"Idempotency-Key": f"provenance-{uuid.uuid4().hex}"}
    try:
        first = client.post(
            "/api/v1/emails/send",
            json=_message(customer_id, email,
                          suggestion_kind="send_follow_up",
                          suggestion_attempt=0),
            headers=key,
        )
        assert first.status_code == 200, first.text

        second = client.post(
            "/api/v1/emails/send",
            json=_message(customer_id, email,
                          suggestion_kind="answer_them",
                          suggestion_attempt=3),
            headers=key,
        )
        assert second.status_code == 200, second.text
        assert second.json()["replayed"] is True
        # The row still describes the first send.
        assert second.json()["suggestion_kind"] == "send_follow_up"
        assert second.json()["suggestion_attempt"] == 0
        assert len(calls) == 1, "the replay sent a second copy"
    finally:
        await _cleanup(db, customer_id)


async def test_mail_without_provenance_stays_without_it(client, db, monkeypatch):
    """Hand-written mail must not be labelled as a suggestion to fill a column."""
    _stub_send(monkeypatch)
    customer_id, email = await _a_customer(db)
    try:
        response = client.post(
            "/api/v1/emails/send", json=_message(customer_id, email)
        )
        assert response.status_code == 200, response.text
        assert response.json()["suggestion_kind"] is None
        assert response.json()["suggestion_attempt"] is None
    finally:
        await _cleanup(db, customer_id)
