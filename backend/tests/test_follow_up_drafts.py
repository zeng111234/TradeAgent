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
from app.llm import LLMTransient


async def _plan_for_a_due_follow_up(db, monkeypatch, complete, *, has_email=True):
    customer = Customer(company_name="Draft Regression Co", country="DE",
                        stage="contacted")
    db.add(customer)
    await db.commit()

    now = datetime.utcnow()
    if has_email:
        sent = now - timedelta(days=4)  # past the 3-day first follow-up interval
        thread = derive_thread_state([SimpleNamespace(
            status="sent", created_at=sent, sent_at=sent, replied_at=None,
        )], now=now)
    else:
        thread = derive_thread_state([], now=now)
    plan_input = CustomerPlanInput(
        customer_id=customer.id,
        company_name=customer.company_name,
        stage="contacted",
        thread=thread,
        contact_email="buyer@example.test",
        contact_name="Buyer",
        has_any_email=has_email,
    )

    async def fake_inputs(_db, _customers, _now):
        return [plan_input]

    monkeypatch.setattr(agent_service, "_plan_inputs_for", fake_inputs)
    monkeypatch.setattr(agent_service, "is_configured", lambda: True)
    monkeypatch.setattr(agent_service, "complete_text", complete)

    try:
        plan = await agent_service._build_follow_up_plan(
            db, due_only=False, persist=False
        )
    finally:
        await db.delete(customer)
        await db.commit()

    return next(
        item for item in plan["actions"] if item["customer_id"] == customer.id
    )


async def test_a_follow_up_action_gets_the_draft_it_asked_for(db, monkeypatch):
    async def fake_complete(*_args, **_kwargs):
        return "<p>Following up on the quote.</p>"

    action = await _plan_for_a_due_follow_up(db, monkeypatch, fake_complete)

    assert action["kind"] == KIND_SEND_FOLLOW_UP
    assert action["draft_email"] == "<p>Following up on the quote.</p>"
    assert action["draft_subject"] == "Following up - Draft Regression Co"
    assert "draft_error" not in action


async def test_a_failed_draft_is_a_field_not_a_missing_field(db, monkeypatch):
    """The failure this test exists for: no `draft_email` and no explanation.

    The exception handler still allows the rest of the plan to be returned, but
    the action must carry `draft_error`. A caller reading the plan cannot see a
    log line.
    """
    async def failing_complete(*_args, **_kwargs):
        raise LLMTransient("the provider returned HTTP 503")

    action = await _plan_for_a_due_follow_up(db, monkeypatch, failing_complete)

    assert action["kind"] == KIND_SEND_FOLLOW_UP
    assert "draft_email" not in action
    assert "draft_error" in action
    assert "LLMTransient" in action["draft_error"]
    # Exactly one of the two, never both and never neither.
    assert ("draft_email" in action) != ("draft_error" in action)


async def test_a_first_contact_is_not_written_as_a_follow_up(db, monkeypatch):
    """A real run produced "my earlier note may not have landed" for a customer
    with zero emails. The prompt must say first contact, and the subject too."""
    captured = {}

    async def capture(prompt, **_kwargs):
        captured["prompt"] = prompt
        return "<p>Hello, we supply metallic yarn.</p>"

    action = await _plan_for_a_due_follow_up(
        db, monkeypatch, capture, has_email=False
    )

    assert action["attempt"] == 0
    assert "first contact" in captured["prompt"]
    assert "no earlier email" in captured["prompt"]
    assert "Write a warm, professional follow-up" not in captured["prompt"]
    # The customer is the recipient. A real run wrote "my name is ... from
    # ABC Fashion Imports" -- the customer's own company -- when the prompt
    # labelled both sides as "Company".
    assert "Your company (the sender)" in captured["prompt"]
    assert "Customer company (the recipient)" in captured["prompt"]
    assert "Never write on behalf" in captured["prompt"]
    assert "Do not invent" in captured["prompt"]
    assert action["draft_subject"].startswith("Introduction")
