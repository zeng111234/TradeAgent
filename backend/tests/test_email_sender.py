"""The SMTP adapter, tested at the library seam.

There is no SMTP test environment here. The only server this product talks to is
the real mailbox, and a test that sends to it is a test that emails a customer.
So what gets pinned is the adapter's *call contract*: which host, which port, TLS
on or off, its own timeout, and the fact that a rejection becomes a result
instead of an exception.

What this cannot prove is that a real server accepts the message. Only delivery
proves that, and delivery is production -- which is why the send path also has
claim rows, a simulated mode and an error log, none of which depend on this
adapter being correct.
"""
from __future__ import annotations

import aiosmtplib
import pytest

from app import config
from app.utils import email_sender


@pytest.fixture
def smtp_configured(monkeypatch):
    monkeypatch.setattr(config.settings, "SMTP_USER", "user@example.test", raising=False)
    monkeypatch.setattr(config.settings, "SMTP_PASSWORD", "secret", raising=False)
    monkeypatch.setattr(config.settings, "SMTP_HOST", "smtp.example.test", raising=False)


@pytest.mark.asyncio
async def test_the_send_carries_the_configured_endpoint_and_its_own_timeout(
    monkeypatch, smtp_configured
):
    calls: list[tuple[object, dict]] = []

    async def fake_send(message, **kwargs):
        calls.append((message, kwargs))

    monkeypatch.setattr(aiosmtplib, "send", fake_send)

    result = await email_sender.send_email(
        "buyer@example.test", "Buyer", "Re: fabric", "Body"
    )

    assert result == {"success": True, "simulated": False}
    message, kwargs = calls[0]
    assert kwargs["hostname"] == "smtp.example.test"
    assert kwargs["port"] == 465
    assert kwargs["use_tls"] is True
    assert kwargs["timeout"] == email_sender.SMTP_SEND_SECONDS
    assert message["To"] == "Buyer <buyer@example.test>"


@pytest.mark.asyncio
async def test_a_rejection_is_a_result_not_an_exception(monkeypatch, smtp_configured):
    async def reject(message, **kwargs):
        raise aiosmtplib.SMTPException("550 mailbox unavailable")

    monkeypatch.setattr(aiosmtplib, "send", reject)

    result = await email_sender.send_email(
        "buyer@example.test", "Buyer", "Re: fabric", "Body"
    )

    assert result["success"] is False
    assert "550" in result["error"]


@pytest.mark.asyncio
async def test_without_credentials_nothing_is_dialled(monkeypatch):
    """``simulated`` must mean "no connection", not "connected and ignored"."""

    async def must_not_be_called(message, **kwargs):
        raise AssertionError("simulated mode opened a connection")

    monkeypatch.setattr(aiosmtplib, "send", must_not_be_called)

    result = await email_sender.send_email(
        "buyer@example.test", "Buyer", "Re: fabric", "Body"
    )

    assert result["success"] is True
    assert result["simulated"] is True
