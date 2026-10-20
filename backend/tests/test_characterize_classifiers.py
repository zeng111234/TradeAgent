"""Characterisation tests for the deterministic classification paths.

These functions are the reason this project is not yet an agent: they are
keyword rules pretending to be judgement. ``_basic_reply_analysis`` is exactly
the kind of decision that should be made by an LLM with tools.

The tests below therefore pin down *current* behaviour, including its
order-dependence — not because the behaviour is good, but so the refactor
cannot change it silently. A test needing an update here is a deliberate
behaviour change, not an accident.
"""
from __future__ import annotations

import pytest

from app.services.ai_service import generate_cold_email
from app.services.agent_service import (
    analyze_email_reply,
    analyze_inquiry,
    get_holiday_calendar,
)


# --------------------------------------------------------------------------
# generate_cold_email — deterministic template fallback when no API key
# --------------------------------------------------------------------------

async def test_cold_email_falls_back_to_template_without_api_key():
    result = await generate_cold_email(
        product_name="gold metallic yarn",
        company_name="Ningbo Test Co",
        target_country="Germany",
    )
    assert set(result) == {
        "subject", "body", "suggestions", "ai_powered", "fallback_reason",
    }
    assert "gold metallic yarn" in result["subject"]
    assert "Ningbo Test Co" in result["body"]
    assert isinstance(result["suggestions"], list) and result["suggestions"]
    # The template is not model output and the response says so.
    assert result["ai_powered"] is False
    assert result["fallback_reason"] == "no_api_key"


async def test_cold_email_template_always_produces_non_empty_body():
    """Even with nothing supplied, the fallback must not crash or return ''."""
    result = await generate_cold_email(product_name="")
    assert result["subject"]  # non-empty
    assert result["body"]


# --------------------------------------------------------------------------
# analyze_email_reply / analyze_inquiry — order-dependent keyword classifiers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "content,expected_intent",
    [
        ("what is the price?", "price_negotiation"),
        ("can you send a sample?", "sample_request"),
        ("no thanks, not interested", "rejection"),
        ("we would like to place an order", "order"),
        ("tell me more about your catalog", "inquiry"),
        ("just checking in", "follow_up"),
    ],
)
async def test_reply_intent_classification(content, expected_intent):
    result = await analyze_email_reply(content)
    assert result["intent"] == expected_intent


async def test_reply_intent_is_first_match_wins():
    """Documents the priority order: price > sample > rejection > order > inquiry.

    A real enquiry that says "please quote your price for a 5000pc order"
    is classified as price_negotiation and never reaches the ``order`` branch.
    This is the pre-refactor behaviour and is pinned on purpose.
    """
    result = await analyze_email_reply("please quote your price for a 5000pc order")
    assert result["intent"] == "price_negotiation"

    # "sample" outranks "order" as well.
    result = await analyze_email_reply("send samples so we can place the order")
    assert result["intent"] == "sample_request"


@pytest.mark.parametrize(
    "content,expected_sentiment",
    [
        ("great, this looks perfect", "positive"),
        ("unfortunately too expensive", "negative"),
        ("received your email", "neutral"),
    ],
)
async def test_reply_sentiment(content, expected_sentiment):
    result = await analyze_email_reply(content)
    assert result["sentiment"] == expected_sentiment


async def test_reply_response_shape_is_stable():
    result = await analyze_email_reply("hello")
    assert set(result) == {
        "intent",
        "sentiment",
        "urgency",
        "extracted_info",
        "customer_mood",
        "recommended_action",
        "suggested_reply_points",
        "deal_stage_update",
        "risks",
        "ai_powered",
        # Added when the boundary between deterministic code and the model was
        # made explicit: this response is keyword rules, and it says so.
        "judgement_source",
        # Added when the fallbacks were made to say *why* they are fallbacks.
        # "no_api_key" here is the real reason (no key in tests); the shape
        # change is deliberate -- see test_llm_provenance.py's analysis section.
        "fallback_reason",
    }
    assert result["ai_powered"] is False  # no API key in tests
    assert result["judgement_source"] == "keyword_rules"
    assert result["fallback_reason"] == "no_api_key"
    assert isinstance(result["extracted_info"], dict)
    # `urgency` used to be the constant "medium" for every message.
    assert result["urgency"] == "not_assessed"


@pytest.mark.parametrize(
    "content,expected_type",
    [
        ("please send quotation", "price_request"),
        ("we need a sample first", "sample_request"),
        ("we want to place another order", "repeat_order"),
        ("we are looking for a supplier", "new_inquiry"),
        ("hello", "general"),
    ],
)
async def test_inquiry_type_classification(content, expected_type):
    result = await analyze_inquiry(content)
    assert result["inquiry_type"] == expected_type


@pytest.mark.parametrize(
    "content,expected_urgency",
    [
        ("urgent, need it asap", "high"),
        ("this is urgent", "high"),
        ("whenever suits you", "low"),
        ("just following up", "medium"),
    ],
)
async def test_inquiry_urgency(content, expected_urgency):
    result = await analyze_inquiry(content)
    assert result["urgency"] == expected_urgency


async def test_inquiry_urgency_negation_is_broken_pinned_quirk():
    """KNOWN BUG, pinned deliberately — do not "fix" by editing this test.

    ``_basic_inquiry_analysis`` decides urgency with::

        if any(w in text_lower for w in ["urgent", "asap", "immediately", "rush"]):
            urgency = "high"
        elif any(w in text_lower for w in ["no rush", "whenever", "at your convenience"]):
            urgency = "low"

    Substring matching means ``"rush"`` matches inside ``"no rush"``, so every
    well-known polite-negation phrase for low urgency is classified HIGH, and
    the ``elif`` branch on line 667 is unreachable dead code.

    Pinned rather than patched because:
      * the refactor mandate is "do not change behaviour", and
      * M4 deletes these keyword rules in favour of an LLM tool call
        (``classify_inquiry``), where negation is handled properly.

    When M4 lands, this test must be DELETED along with the rules it pins.
    """
    for phrase in ("no rush", "we are in no rush", "please take your time, no rush"):
        result = await analyze_inquiry(phrase)
        assert result["urgency"] == "high", (
            "pinned bug: substring 'rush' inside 'no rush' yields high urgency"
        )


async def test_inquiry_low_urgency_branch_is_reachable_only_without_rush_substring():
    """The only way to hit the 'low' branch is a phrase with no high-urgency substring."""
    assert (await analyze_inquiry("at your convenience"))["urgency"] == "low"
    assert (await analyze_inquiry("whenever suits you"))["urgency"] == "low"


async def test_inquiry_shipping_term_extraction():
    assert (await analyze_inquiry("our terms are FOB Ningbo"))["delivery_requirements"]["shipping_terms"] == "FOB"
    assert (await analyze_inquiry("please quote CIF Hamburg"))["delivery_requirements"]["shipping_terms"] == "CIF"
    assert (await analyze_inquiry("hello"))["delivery_requirements"]["shipping_terms"] == "Not mentioned"


async def test_inquiry_response_shape_is_stable():
    result = await analyze_inquiry("hello")
    assert set(result) == {
        "inquiry_type",
        "customer_info",
        "products",
        "delivery_requirements",
        "urgency",
        "urgency_reason",
        "customer_intent_score",
        "intent_score_reason",
        "key_concerns",
        "missing_info",
        "suggested_reply_draft",
        "suggested_reply_subject",
        "follow_up_strategy",
        "ai_powered",
        # Added when the boundary between deterministic code and the model was
        # made explicit: this response is keyword rules, and it says so.
        "judgement_source",
        # Added when the silent fallback was closed: the response now says why
        # it is keyword rules, not only that it is.
        "fallback_reason",
    }
    assert result["judgement_source"] == "keyword_rules"
    assert result["fallback_reason"] == "no_api_key"


async def test_the_inquiry_fallback_stops_scoring_intent():
    """It used to return 50 for every inquiry, from a constant.

    A number in a score field is read as an assessment. Two different inquiries
    -- one a one-line hello, one a firm order with a deadline -- got the same
    number, because the number was never derived from anything.
    """
    hello = await analyze_inquiry("hello")
    order = await analyze_inquiry("we need 5000 units urgently, our deadline is Friday")

    assert hello["customer_intent_score"] is None
    assert order["customer_intent_score"] is None
    assert "not assessed" in hello["intent_score_reason"]


# --------------------------------------------------------------------------
# get_holiday_calendar — pure function
# --------------------------------------------------------------------------

def test_holiday_calendar_is_deterministic_and_shaped():
    first = get_holiday_calendar()
    second = get_holiday_calendar()
    assert isinstance(first, list) and first
    # Same call twice must give the same holiday names (dates may shift).
    assert [h.get("name") for h in first] == [h.get("name") for h in second]
