"""Optional enrichment may fail; the failure must be a field, not an absence.

The class of defect this pins: an `except Exception` that logs and continues
without writing anything into the result. The caller sees a plan/alert/email
with a missing key and cannot tell "this action needed no draft" from "the
draft failed". `EmailLog` was one instance; rescue mail and holiday mail had the
same shape.

Each test stubs the model to fail and asserts the *product* says why.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from app.llm import LLMTransient
from app.models.customer import Customer, CustomerContact
from app.services import agent_service


async def _a_customer_at_risk(db):
    customer = Customer(
        company_name=f"Partial Result {uuid4().hex[:8]}",
        country="DE",
        stage="quoting",
        created_at=datetime.utcnow() - timedelta(days=100),
        updated_at=datetime.utcnow() - timedelta(days=100),
    )
    db.add(customer)
    await db.flush()
    contact = CustomerContact(
        customer_id=customer.id,
        name="Buyer",
        email=f"buyer-{uuid4().hex[:8]}@example.test",
        is_primary=1,
    )
    db.add(contact)
    await db.commit()
    return customer


async def _cleanup(db, customer):
    await db.delete(customer)
    await db.commit()


async def test_a_failed_rescue_email_is_reported_on_the_alert(monkeypatch):
    """One synthetic alert, so the assertion cannot depend on the top-five cap."""
    async def failing_complete(*_args, **_kwargs):
        raise LLMTransient("the provider returned HTTP 503")

    monkeypatch.setattr(agent_service, "is_configured", lambda: True)
    monkeypatch.setattr(agent_service, "complete_text", failing_complete)

    alert = {
        "customer_id": 42,
        "company_name": "At Risk Co",
        "stage": "quoting",
        "risk_level": "high",
        "risk_reasons": ["Stuck in quoting stage"],
        "contact_email": "buyer@example.test",
        "last_contacted": None,
    }

    await agent_service._attach_rescue_drafts([alert], datetime.utcnow())

    assert "rescue_email" not in alert
    assert "rescue_error" in alert
    assert "LLMTransient" in alert["rescue_error"]


async def test_a_failed_holiday_personalisation_falls_back_with_a_reason(db, monkeypatch):
    customer = await _a_customer_at_risk(db)

    async def failing_json(*_args, **_kwargs):
        raise LLMTransient("the provider returned HTTP 503")

    monkeypatch.setattr(agent_service, "is_configured", lambda: True)
    monkeypatch.setattr(agent_service, "complete_json", failing_json)

    try:
        result = await agent_service.generate_holiday_emails(
            "Christmas", company_name="Us", db_session=db
        )
    finally:
        await _cleanup(db, customer)

    email = next(item for item in result["emails"] if item["customer_id"] == customer.id)
    assert email["personalized"] is False
    assert "LLMTransient" in email["fallback_reason"]
    # The template is still usable; the label is what makes it honest.
    assert email["body"]


async def test_a_failed_inquiry_analysis_says_why_it_is_keyword_based(monkeypatch):
    async def failing_json(*_args, **_kwargs):
        raise LLMTransient("the provider returned HTTP 503")

    monkeypatch.setattr(agent_service, "is_configured", lambda: True)
    monkeypatch.setattr(agent_service, "complete_json", failing_json)

    result = await agent_service.analyze_inquiry("Please quote 500 kg of gold yarn.")

    assert result["ai_powered"] is False
    assert result["judgement_source"] == "keyword_rules"
    assert "LLMTransient" in result["fallback_reason"]


async def test_a_keyword_analysis_without_a_key_says_no_api_key(monkeypatch):
    monkeypatch.setattr(agent_service, "is_configured", lambda: False)

    result = await agent_service.analyze_inquiry("Please quote 500 kg of gold yarn.")

    assert result["fallback_reason"] == "no_api_key"
