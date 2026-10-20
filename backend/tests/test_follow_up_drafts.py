"""The draft attached to a follow-up action -- the path a real run found broken.

`_build_follow_up_plan` caught every exception around the draft call and logged
it, so a `NameError: EmailLog is not defined` looked exactly like "the model
failed": the plan still returned, the action was still there, and only the
`draft_email` field was missing. A real DeepSeek run surfaced it; the test below
is the smallest state that reaches that line.

The exception handler stays (one bad draft must not abort the whole plan), so
the assertion has to be on the *product*: the action carries a draft.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.models.customer import Customer
from app.services import agent_service
from app.services.follow_up_plan import KIND_SEND_FOLLOW_UP, CustomerPlanInput
from app.services.follow_up_policy import derive_thread_state


async def test_a_follow_up_action_gets_the_draft_it_asked_for(db, monkeypatch):
    customer = Customer(company_name="Draft Regression Co", country="DE",
                        stage="contacted")
    db.add(customer)
    await db.commit()

    now = datetime.utcnow()
    sent = now - timedelta(days=4)  # past the 3-day first follow-up interval
    thread = derive_thread_state([SimpleNamespace(
        status="sent", created_at=sent, sent_at=sent, replied_at=None,
    )], now=now)
    plan_input = CustomerPlanInput(
        customer_id=customer.id,
        company_name=customer.company_name,
        stage="contacted",
        thread=thread,
        contact_email="buyer@example.test",
        contact_name="Buyer",
        has_any_email=True,
    )

    async def fake_inputs(_db, _customers, _now):
        return [plan_input]

    async def fake_complete(*_args, **_kwargs):
        return "<p>Following up on the quote.</p>"

    monkeypatch.setattr(agent_service, "_plan_inputs_for", fake_inputs)
    monkeypatch.setattr(agent_service, "is_configured", lambda: True)
    monkeypatch.setattr(agent_service, "complete_text", fake_complete)

    try:
        plan = await agent_service._build_follow_up_plan(
            db, due_only=False, persist=False
        )
    finally:
        await db.delete(customer)
        await db.commit()

    action = next(
        item for item in plan["actions"] if item["customer_id"] == customer.id
    )
    assert action["kind"] == KIND_SEND_FOLLOW_UP
    assert action["draft_email"] == "<p>Following up on the quote.</p>"
    assert action["draft_subject"] == f"Following up - {customer.company_name}"
