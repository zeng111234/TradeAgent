"""The agent's policy tool must agree with the endpoint that uses the same policy.

This is the test that makes the refactor worth doing. Before it, the agent read
raw signals and judged for itself while the endpoints applied hardcoded
thresholds — two paths that could disagree about the same customer, with neither
able to explain the other. Now there is one set of thresholds and this asserts
both callers reach the same verdict.
"""
from __future__ import annotations

import uuid

import pytest

from app.agent.trade_tools import get_customer_assessment
from app.services.agent_service import customer_churn_alerts
from app.services.follow_up_policy import (
    DEFAULT_CHURN_POLICY,
    DEFAULT_FOLLOW_UP_POLICY,
)

pytestmark = pytest.mark.usefixtures("client")


async def _make_customer(db, **overrides):
    """A customer with a controllable shape, so each rule can be reached."""
    from app.models.customer import Customer

    values = dict(
        company_name=f"Pytest Policy Co {uuid.uuid4().hex[:8]}",
        country="Germany",
        stage="contacted",
        score=50.0,
    )
    values.update(overrides)
    row = Customer(**values)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _add_email(db, customer_id, *, status, days_ago):
    from datetime import datetime, timedelta

    from app.models.email_log import EmailLog

    row = EmailLog(
        customer_id=customer_id,
        to_email="buyer@example.com",
        subject="Enquiry",
        body="<p>Hello</p>",
        status=status,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    # created_at has a column default, so set it explicitly and persist.
    row.created_at = datetime.utcnow() - timedelta(days=days_ago)
    await db.commit()
    return row


async def _cleanup(db, customer_id):
    from sqlalchemy import delete

    from app.models.customer import Customer
    from app.models.email_log import EmailLog

    await db.execute(delete(EmailLog).where(EmailLog.customer_id == customer_id))
    await db.execute(delete(Customer).where(Customer.id == customer_id))
    await db.commit()


# --- shape -----------------------------------------------------------------

async def test_assessment_returns_a_verdict_and_the_thresholds(db):
    customer = await _make_customer(db, stage="quoting")
    try:
        result = await get_customer_assessment(customer_id=customer.id)

        assert result["customer_id"] == customer.id
        assert set(result) >= {"risk", "follow_up", "inputs", "thresholds_applied", "note"}

        assert "level" in result["risk"]
        assert "at_risk" in result["risk"]
        assert isinstance(result["risk"]["reasons"], list)
        assert isinstance(result["risk"]["suggested_actions"], list)

        # The thresholds are exposed, not hidden behind the verdict.
        thresholds = result["thresholds_applied"]
        assert thresholds["no_reply_critical_emails"] == DEFAULT_CHURN_POLICY.no_reply_critical_emails
        assert thresholds["silent_warm_after_days"] == DEFAULT_CHURN_POLICY.silent_warm_after_days
        assert thresholds["follow_up_quoting_is_always"] == DEFAULT_FOLLOW_UP_POLICY.quoting_always_priority
    finally:
        await _cleanup(db, customer.id)


async def test_assessment_says_it_is_a_policy_not_the_truth(db):
    """A fixed rule presented bare invites the model to treat it as ground truth."""
    customer = await _make_customer(db)
    try:
        result = await get_customer_assessment(customer_id=customer.id)
        note = result["note"].lower()
        assert "not ground truth" in note
        assert "disagree" in note
        # And it warns that the two policies can contradict each other.
        assert "churn" in note and "follow-up" in note
    finally:
        await _cleanup(db, customer.id)


async def test_assessment_handles_a_missing_customer(db):
    assert "error" in await get_customer_assessment(customer_id=999_999)


# --- the unification -------------------------------------------------------

async def test_the_agent_tool_agrees_with_the_endpoint_verdict(db):
    """Same policy, same data, so the same level — that is the whole point.

    Before this the agent judged for itself and could recommend chasing someone
    the dashboard had not flagged. Now a disagreement has to be an explicit
    disagreement with a threshold the agent can name.
    """
    # Three emails, no reply -> the churn policy calls this critical.
    customer = await _make_customer(db, stage="contacted")
    try:
        for _ in range(3):
            await _add_email(db, customer.id, status="sent", days_ago=5)

        assessment = await get_customer_assessment(customer_id=customer.id)
        assert assessment["risk"]["level"] == "critical"

        alerts = await customer_churn_alerts(db_session=db)
        matching = [a for a in alerts["alerts"] if a["customer_id"] == customer.id]
        assert matching, "the endpoint did not flag a customer the tool called critical"
        assert matching[0]["risk_level"] == assessment["risk"]["level"]
    finally:
        await _cleanup(db, customer.id)


async def test_the_agent_tool_agrees_when_neither_flags(db):
    """A quiet customer must be quiet on both paths."""
    customer = await _make_customer(db, stage="quoting")
    try:
        await _add_email(db, customer.id, status="replied", days_ago=1)

        assessment = await get_customer_assessment(customer_id=customer.id)
        alerts = await customer_churn_alerts(db_session=db)
        matching = [a for a in alerts["alerts"] if a["customer_id"] == customer.id]

        assert assessment["risk"]["at_risk"] == bool(matching), (
            "the tool and the endpoint disagree about whether this customer is at risk"
        )
    finally:
        await _cleanup(db, customer.id)


async def test_follow_up_and_churn_answers_are_both_present_even_when_they_differ(db):
    """The documented conflict, made visible through the tool.

    A quote sent today is `high` priority for follow-up by rule, while the churn
    policy sees no silence to act on. Both answers come back, so the agent can see
    that the question differs rather than getting one number and assuming it is
    the whole picture.
    """
    customer = await _make_customer(db, stage="quoting")
    try:
        assert (await get_customer_assessment(customer_id=customer.id))["follow_up"] is not None

        result = await get_customer_assessment(customer_id=customer.id)
        assert result["follow_up"]["priority"] == "high"
        # No silence, so churn does not fire. The two answers differ and both are
        # correct for their own question.
        assert result["risk"]["at_risk"] is False
    finally:
        await _cleanup(db, customer.id)


async def test_assessment_is_available_to_the_agent_registry():
    from app.agent.service import build_registry

    registry = build_registry()
    assert "get_customer_assessment" in registry
    # It reads; it must not be able to write.
    assert "write" not in registry.get("get_customer_assessment").tags
