"""Where the boundary between deterministic code and the model sits, enforced.

The rule this file exists to make executable:

    Deterministic code owns what **exists**, what is **allowed**, and what is
    **known**: the schema, argument validation, budgets, the transcript, the
    scheduling arithmetic, the named thresholds, and the write boundary.

    The model owns what **matters** and what to **say**: ranking, scoring,
    classification, wording, whether to delegate, when to stop.

    A number or a label a human will act on must therefore come from one of two
    places: the model, with the evidence it used, or a **named threshold** in a
    policy module that a reader can find and argue with. Never a bare constant
    sitting in a branch.

The boundary is easy to state and easy to erode, because eroding it never looks
like erosion. It looks like a sensible default. Three real examples from this
codebase, all found by the check below:

* ``_basic_inquiry_analysis`` returned ``customer_intent_score: 50`` -- every
  inquiry scored exactly 50, from a one-line "hello" to a firm order with a
  deadline. The field is read as an assessment; the number was a constant.
* ``analyze_customer_website`` returned ``score: 0`` when the fetch failed. A
  page nobody could read is not a worthless lead, and 0 sorts to the bottom.
* ``_basic_reply_analysis`` returned ``urgency: "medium"`` -- not derived from
  the text, not derived from anything.

So the check is mechanical, and it runs on every test run rather than on the
good intentions of whoever adds the next fallback.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
APP = ROOT / "backend" / "app"

#: Field names that a human reads as an assessment. A constant in one of these
#: is either a fabrication or a "we did not assess this" marker.
JUDGEMENT_KEYS = {
    "score", "lead_score", "relevance_score", "customer_intent_score",
    "intent_score", "risk_level", "priority", "intent", "sentiment", "urgency",
    "inquiry_type", "recommendation", "verdict", "decision",
    "deal_stage_update", "company_size_estimate", "estimated_order_potential",
    "lead_score_reason",
}

#: The vocabulary of "nobody assessed this". These are the *only* constants
#: allowed in a judgement field, because they do not claim to be a judgement.
NOT_ASSESSED = {None, "", "unknown", "not_assessed", "not_analyzed", "not_mentioned"}

#: A statement carrying this mark is exempt, and the exemption has to be visible
#: at the site. Used for the tool that returns the *shape* of the answer it wants
#: from the model -- the opposite of deterministic code making the judgement.
CONTRACT_MARK = "judgement-boundary: contract"


def _source_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _python_files() -> list[Path]:
    return [
        p for p in sorted(APP.rglob("*.py")) if "__pycache__" not in p.parts
    ]


def _is_not_assessed(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in NOT_ASSESSED


def _constant_judgements(path: Path) -> list[str]:
    """Every judgement field in this file whose value is a bare constant."""
    lines = _source_lines(path)
    tree = ast.parse("\n".join(lines))
    found: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        # The mark may sit anywhere in the statement that introduces the dict,
        # including a comment block above it.
        window = "\n".join(lines[max(0, node.lineno - 15): node.lineno + 1])
        if CONTRACT_MARK in window:
            continue
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if key.value.lower() not in JUDGEMENT_KEYS:
                continue
            if _is_not_assessed(value):
                continue
            if isinstance(value, ast.Constant):
                found.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}  "
                    f"{key.value!r}: {ast.unparse(value)}"
                )
    return found


def test_no_deterministic_code_answers_a_judgement_question():
    """The rule, over the whole application."""
    offenders: list[str] = []
    for path in _python_files():
        offenders.extend(_constant_judgements(path))

    assert not offenders, (
        "a judgement field is holding a constant. Either take the value from the "
        "model with the evidence it used, put the threshold in a policy module "
        "with a name, or say 'not assessed':\n  " + "\n  ".join(offenders)
    )


def test_the_check_actually_finds_a_fabrication():
    """A guard nobody has seen fail is a guard nobody should trust."""
    sample = '''
def analyse(text):
    intent = "price_negotiation"
    return {"intent": intent, "customer_intent_score": 50}
'''
    tree = ast.parse(sample)
    judgements = [
        key.value
        for node in ast.walk(tree) if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and key.value.lower() in JUDGEMENT_KEYS
        and isinstance(value, ast.Constant) and value.value not in NOT_ASSESSED
    ]

    assert judgements == ["customer_intent_score"], (
        "the detector no longer recognises a constant score, so the real check "
        "proves nothing"
    )


# --------------------------------------------------------------------------
# The specific fabrications, pinned so they cannot come back
# --------------------------------------------------------------------------

def test_the_inquiry_fallback_does_not_invent_an_intent_score():
    from app.services.agent_service import _basic_inquiry_analysis

    result = _basic_inquiry_analysis("urgent, we need 5000 units of gold thread")

    assert result["customer_intent_score"] is None, (
        "a number in this field is read as an assessment; a keyword rule cannot "
        "produce one"
    )
    assert "not assessed" in result["intent_score_reason"]
    # What it *can* see from the text is still there, and still honest.
    assert result["urgency"] == "high"
    assert result["delivery_requirements"]["shipping_terms"]


def test_the_reply_fallback_does_not_invent_an_urgency():
    from app.services.agent_service import _basic_reply_analysis

    result = _basic_reply_analysis("thanks, this looks great")

    assert result["urgency"] == "not_assessed", (
        "urgency was the constant 'medium' for every message, whatever it said"
    )
    assert result["judgement_source"] == "keyword_rules"
    assert result["ai_powered"] is False


def test_a_failed_fetch_is_not_a_zero_score():
    import asyncio

    import requests

    from app.services.agent_service import analyze_customer_website

    def refuse(*args, **kwargs):
        raise OSError("no route to host")

    original = requests.get
    requests.get = refuse
    try:
        result = asyncio.run(analyze_customer_website("https://nope.example"))
    finally:
        requests.get = original

    assert "error" in result
    assert result["score"] is None, (
        "0 is an assessment, and it sorts to the bottom of a ranked list"
    )


# --------------------------------------------------------------------------
# The other half: the model's side of the boundary stays the model's
# --------------------------------------------------------------------------

def test_the_scoring_tool_asks_for_a_score_instead_of_producing_one():
    import asyncio

    from app.agent.trade_tools import classify_prospect

    result = asyncio.run(classify_prospect(
        page_text="We import embroidery thread for the German market.",
        target_products="gold metallic yarn",
    ))

    assert "score" not in result, "the tool must not answer the judgement itself"
    assert "recommendation" not in result
    assert "score" in result["required_output"], (
        "it must still ask the model for one, with the rubric to judge against"
    )


def test_customer_signals_returns_numbers_and_not_a_verdict():
    import asyncio

    from app.agent.trade_tools import get_customer_signals

    result = asyncio.run(get_customer_signals(customer_id=1))

    for key in ("risk_level", "risk_reasons", "suggested_actions", "priority"):
        assert key not in result, (
            f"{key!r} would be a verdict; the raw signals are the tool's job"
        )


def test_the_schedule_is_arithmetic_and_not_a_model_call():
    """The other side of the same boundary: some things must not be judgement.

    A due date is a computation. If a model decided it, the same input would
    produce different dates on different days, and nobody could explain why a
    customer was scheduled for Tuesday.
    """
    import inspect

    from app.services import follow_up_plan

    source = inspect.getsource(follow_up_plan)
    for forbidden in ("complete_text", "complete_json", "get_client", "llm"):
        assert forbidden not in source, (
            f"follow_up_plan refers to {forbidden!r}; the schedule must stay "
            "arithmetic so it is reproducible and explainable"
        )
