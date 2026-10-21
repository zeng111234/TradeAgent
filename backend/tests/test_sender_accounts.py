"""Switchable sender mailboxes: the boundary, and the switching itself.

The seam that matters is ``aiosmtplib.send`` -- patched here, as in
``test_email_sender.py``, because a test that really sends is a test that
emails a customer. What that leaves provable is the part the operator cares
about: which host, which port, which From address a send carries, that
switching changes exactly those and nothing else, and that the password never
travels back over HTTP.
"""
from __future__ import annotations

import uuid

import aiosmtplib
import pytest
from sqlalchemy import delete

from app.database import async_session
from app.models.email_log import EmailLog, EmailStatus
from app.models.smtp_account import SmtpAccount
from app.services import smtp_accounts

#: Drafts this file creates carry this prefix, so the cleanup below can tell
#: them from the demo seed and from any other test file's rows.
DRAFT_TAG = "senders-test"


def a_mailbox(**overrides) -> dict:
    """A valid create payload. Distinctive values, so a test that asserts the
    wrong account was used fails loudly instead of quietly matching."""
    fields = dict(
        label="QQ main",
        host="smtp.qq.com",
        port=465,
        use_tls=True,
        username="2421258616@qq.com",
        password="authcode-one",
    )
    fields.update(overrides)
    return fields


async def a_pending_draft(db, *, to_email="buyer@example.test") -> EmailLog:
    row = EmailLog(
        customer_id=None,
        to_email=to_email,
        to_name="Buyer",
        subject="Re: fabric",
        body="<p>Body</p>",
        status=EmailStatus.PENDING,
        tracking_id=f"{DRAFT_TAG}-{uuid.uuid4().hex[:12]}",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture(autouse=True)
async def only_these_mailboxes():
    """The database outlives a test file; these rows must not.

    Mailboxes above all: an active row left behind would silently decide
    where the *next* file's sends go, which is the coupling this fixture
    exists to prevent. Drafts are cleaned by their tag -- the demo seed and
    other files' rows are not mine to remove.
    """
    async with async_session() as db:
        await db.execute(delete(SmtpAccount))
        await db.execute(delete(EmailLog).where(EmailLog.tracking_id.like(f"{DRAFT_TAG}-%")))
        await db.commit()
    yield
    async with async_session() as db:
        await db.execute(delete(SmtpAccount))
        await db.execute(delete(EmailLog).where(EmailLog.tracking_id.like(f"{DRAFT_TAG}-%")))
        await db.commit()


@pytest.fixture
def dial(monkeypatch):
    """Capture the SMTP handshake instead of performing it."""
    calls: list[tuple[object, dict]] = []

    async def fake_send(message, **kwargs):
        calls.append((message, kwargs))

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    return calls


# --------------------------------------------------------------------------
# The password never comes back out
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_creating_a_mailbox_does_not_return_the_password(client):
    body = a_mailbox()
    created = client.post("/api/v1/senders", json=body)
    assert created.status_code == 201, created.text
    assert "password" not in created.json()
    assert created.json()["username"] == body["username"]
    assert created.json()["is_active"] is False


@pytest.mark.asyncio
async def test_listing_mailboxes_does_not_return_the_password(client):
    client.post("/api/v1/senders", json=a_mailbox())
    client.post("/api/v1/senders", json=a_mailbox(label="163 spare", host="smtp.163.com"))
    listed = client.get("/api/v1/senders").json()
    assert len(listed) == 2
    for row in listed:
        assert "password" not in row


# --------------------------------------------------------------------------
# Switching
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_switching_leaves_exactly_one_active(client):
    first = client.post("/api/v1/senders", json=a_mailbox()).json()
    second = client.post(
        "/api/v1/senders", json=a_mailbox(label="163 spare", host="smtp.163.com")
    ).json()

    switched = client.post(f"/api/v1/senders/{second['id']}/activate")
    assert switched.status_code == 200
    assert switched.json()["is_active"] is True

    active = [r for r in client.get("/api/v1/senders").json() if r["is_active"]]
    assert [r["id"] for r in active] == [second["id"]]
    assert first["id"] not in [r["id"] for r in active]


@pytest.mark.asyncio
async def test_the_database_refuses_two_active_mailboxes(db):
    """The service switches in one transaction; the index is what makes that a
    guarantee rather than a habit. Two actives is not a state anything may
    produce, not even a caller that skips the service."""
    db.add(SmtpAccount(label="a", username="a@example.test", password="x", is_active=True))
    db.add(SmtpAccount(label="b", username="b@example.test", password="y", is_active=True))
    with pytest.raises(Exception):  # IntegrityError from the partial unique index
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_creating_with_activate_switches_immediately(client):
    first = client.post("/api/v1/senders", json=a_mailbox(activate=True)).json()
    assert first["is_active"] is True
    second = client.post(
        "/api/v1/senders", json=a_mailbox(label="163 spare", activate=True)
    ).json()
    active = [r for r in client.get("/api/v1/senders").json() if r["is_active"]]
    assert [r["id"] for r in active] == [second["id"]]


@pytest.mark.asyncio
async def test_activating_an_unknown_mailbox_is_a_404(client):
    assert client.post("/api/v1/senders/999999/activate").status_code == 404


@pytest.mark.asyncio
async def test_deleting_an_unknown_mailbox_is_a_404(client):
    assert client.delete("/api/v1/senders/999999").status_code == 404


@pytest.mark.asyncio
async def test_deleting_the_active_mailbox_leaves_none_active(client):
    """No silent promotion: the next send falls back to the environment, and
    if that is unconfigured it is simulated. Both are said out loud."""
    account = client.post("/api/v1/senders", json=a_mailbox(activate=True)).json()
    assert client.delete(f"/api/v1/senders/{account['id']}").status_code == 204
    assert client.get("/api/v1/senders").json() == []
    assert client.get("/api/v1/senders/effective").json()["effective"] == "simulated"


# --------------------------------------------------------------------------
# The switch is what a send actually uses
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_active_mailbox_is_the_one_a_send_uses(client, db, dial):
    """The point of the whole table: press send, it leaves from the active
    mailbox -- host, port, TLS and From, all of it."""
    account = client.post(
        "/api/v1/senders",
        json=a_mailbox(
            label="163 spare",
            host="smtp.163.com",
            port=25,
            use_tls=False,
            username="trade@163.com",
            password="authcode-two",
            activate=True,
        ),
    ).json()

    draft = await a_pending_draft(db)
    sent = client.post(f"/api/v1/emails/send-pending/{draft.id}")
    assert sent.status_code == 200, sent.text

    assert len(dial) == 1
    message, kwargs = dial[0]
    assert kwargs["hostname"] == "smtp.163.com"
    assert kwargs["port"] == 25
    assert kwargs["use_tls"] is False
    assert kwargs["username"] == "trade@163.com"
    assert kwargs["password"] == "authcode-two"
    assert message["From"] == "trade@163.com"
    assert account["username"] == "trade@163.com"


@pytest.mark.asyncio
async def test_switching_changes_the_sender_of_the_next_send(client, db, dial):
    client.post(
        "/api/v1/senders",
        json=a_mailbox(username="2421258616@qq.com", password="authcode-one", activate=True),
    )
    spare = client.post(
        "/api/v1/senders",
        json=a_mailbox(label="163 spare", host="smtp.163.com", username="trade@163.com"),
    ).json()

    first = await a_pending_draft(db, to_email="one@example.test")
    assert client.post(f"/api/v1/emails/send-pending/{first.id}").status_code == 200
    assert dial[0][1]["username"] == "2421258616@qq.com"

    client.post(f"/api/v1/senders/{spare['id']}/activate")

    second = await a_pending_draft(db, to_email="two@example.test")
    assert client.post(f"/api/v1/emails/send-pending/{second.id}").status_code == 200
    assert dial[1][1]["username"] == "trade@163.com"
    assert dial[1][0]["From"] == "trade@163.com"


@pytest.mark.asyncio
async def test_an_edit_without_a_password_keeps_the_stored_one(client, db, dial):
    """Rotating a label must not require digging the authorization code out
    again -- or the next edit happens in the database instead of the UI."""
    created = client.post("/api/v1/senders", json=a_mailbox(activate=True)).json()
    edited = client.put(
        f"/api/v1/senders/{created['id']}", json={"label": "renamed"}
    )
    assert edited.status_code == 200
    assert edited.json()["label"] == "renamed"
    assert "password" not in edited.json()

    draft = await a_pending_draft(db)
    assert client.post(f"/api/v1/emails/send-pending/{draft.id}").status_code == 200
    assert dial[0][1]["password"] == "authcode-one"


@pytest.mark.asyncio
async def test_with_no_account_the_environment_still_decides(client, db, dial, monkeypatch):
    """Yesterday's behaviour, unchanged: no rows, so the environment is the
    source. This is what keeps the upgrade invisible to an operator who has
    not opened the new page."""
    from app import config

    monkeypatch.setattr(config.settings, "SMTP_USER", "env@example.test", raising=False)
    monkeypatch.setattr(config.settings, "SMTP_PASSWORD", "env-secret", raising=False)
    monkeypatch.setattr(config.settings, "SMTP_HOST", "smtp.env.test", raising=False)

    draft = await a_pending_draft(db)
    assert client.post(f"/api/v1/emails/send-pending/{draft.id}").status_code == 200

    assert dial[0][1]["hostname"] == "smtp.env.test"
    assert dial[0][1]["username"] == "env@example.test"
    assert dial[0][0]["From"] == "env@example.test"


@pytest.mark.asyncio
async def test_an_account_beats_the_environment(client, db, dial, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "SMTP_USER", "env@example.test", raising=False)
    monkeypatch.setattr(config.settings, "SMTP_PASSWORD", "env-secret", raising=False)
    monkeypatch.setattr(config.settings, "SMTP_HOST", "smtp.env.test", raising=False)
    client.post("/api/v1/senders", json=a_mailbox(activate=True))

    draft = await a_pending_draft(db)
    assert client.post(f"/api/v1/emails/send-pending/{draft.id}").status_code == 200

    assert dial[0][1]["hostname"] == "smtp.qq.com"
    assert dial[0][1]["username"] == "2421258616@qq.com"


@pytest.mark.asyncio
async def test_with_nothing_configured_the_send_is_simulated(client, db):
    """No dial at all -- ``simulated`` must mean "no connection", not
    "connected and ignored"."""
    draft = await a_pending_draft(db)
    sent = client.post(f"/api/v1/emails/send-pending/{draft.id}").json()
    assert sent["status"] == "sent"
    assert sent["simulated"] is True


# --------------------------------------------------------------------------
# What a send would use, without sending
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_effective_endpoint_names_the_source(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "SMTP_USER", "", raising=False)
    monkeypatch.setattr(config.settings, "SMTP_PASSWORD", "", raising=False)

    status = client.get("/api/v1/senders/effective").json()
    assert status == {
        "effective": "simulated",
        "active_account_id": None,
        "env_configured": False,
    }

    monkeypatch.setattr(config.settings, "SMTP_USER", "env@example.test", raising=False)
    monkeypatch.setattr(config.settings, "SMTP_PASSWORD", "env-secret", raising=False)
    assert client.get("/api/v1/senders/effective").json()["effective"] == "env"

    account = client.post("/api/v1/senders", json=a_mailbox(activate=True)).json()
    status = client.get("/api/v1/senders/effective").json()
    assert status["effective"] == "account"
    assert status["active_account_id"] == account["id"]
    assert status["env_configured"] is True


@pytest.mark.asyncio
async def test_the_resolver_prefers_the_active_row_over_the_environment(db, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "SMTP_USER", "env@example.test", raising=False)
    monkeypatch.setattr(config.settings, "SMTP_PASSWORD", "env-secret", raising=False)
    monkeypatch.setattr(config.settings, "SMTP_HOST", "smtp.env.test", raising=False)

    db.add(SmtpAccount(
        label="row", username="row@example.test", password="row-secret",
        host="smtp.row.test", port=587, use_tls=False, is_active=True,
    ))
    await db.commit()

    resolved = await smtp_accounts.resolve_active_sender(db)
    assert resolved is not None
    assert resolved.source == "account"
    assert resolved.host == "smtp.row.test"
    assert resolved.port == 587
    assert resolved.username == "row@example.test"
