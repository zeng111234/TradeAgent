"""The canary's assertions, exercised without a provider.

The canary only earns its keep if it fails for the right reason. These tests feed
it the responses a drifting provider would produce -- a content list, a renamed
usage block, a tool call with unparseable arguments -- and assert that each one is
named rather than averaged into "the call worked".

A canary that cannot recognise drift is worse than none: it is a green light with
a cron expression attached.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.canary import (
    DRIFT,
    OK,
    build_record,
    check_text_reply,
    check_tool_reply,
    verdict,
)


def _reply(**overrides):
    values = {
        "content": "ok",
        "tool_calls": [],
        "prompt_tokens": 12,
        "completion_tokens": 1,
        "finish_reason": "stop",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _call(name="report_city", arguments='{"city": "Berlin"}'):
    return SimpleNamespace(id="call_1", name=name, arguments=arguments)


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

def test_a_healthy_text_answer_has_no_problems():
    assert check_text_reply(_reply()) == []


def test_content_as_parts_is_drift_named_as_a_shape_change():
    problems = check_text_reply(_reply(content=[{"type": "text", "text": "ok"}]))
    assert problems
    assert any("response shape" in problem for problem in problems)


def test_empty_content_is_drift():
    assert check_text_reply(_reply(content="   ")) != []


def test_zero_usage_is_drift_not_a_free_call():
    problems = check_text_reply(_reply(prompt_tokens=0, completion_tokens=0))
    assert len(problems) == 2
    assert any("ledger" in problem for problem in problems), problems


def test_a_truncated_one_word_answer_is_drift():
    problems = check_text_reply(_reply(finish_reason="length"))
    assert any("finish_reason" in problem for problem in problems)


# --------------------------------------------------------------------------
# Tool calls
# --------------------------------------------------------------------------

def test_a_well_formed_tool_call_has_no_problems():
    assert check_tool_reply(_reply(tool_calls=[_call()])) == []


def test_no_tool_calls_is_drift():
    problems = check_tool_reply(_reply(tool_calls=[]))
    assert any("no tool_calls" in problem for problem in problems)


def test_an_empty_tool_name_is_drift():
    problems = check_tool_reply(_reply(tool_calls=[_call(name="")]))
    assert any("empty name" in problem for problem in problems)


def test_the_wrong_tool_name_is_drift():
    problems = check_tool_reply(_reply(tool_calls=[_call(name="get_weather")]))
    assert any("get_weather" in problem for problem in problems)


def test_unparseable_arguments_are_drift():
    problems = check_tool_reply(_reply(tool_calls=[_call(arguments="{city:")]))
    assert any("not valid JSON" in problem for problem in problems)


def test_arguments_that_are_not_an_object_are_drift():
    problems = check_tool_reply(_reply(tool_calls=[_call(arguments="[1, 2]")]))
    assert any("not an object" in problem for problem in problems)


def test_a_missing_required_key_is_drift():
    problems = check_tool_reply(_reply(tool_calls=[_call(arguments="{}")]))
    assert any("city" in problem for problem in problems)


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------

def test_the_record_is_ok_only_when_every_check_is_ok():
    record = build_record(
        base_url="https://example.test/v1", model="m",
        checked_at="2026-10-20T00:00:00+00:00",
        checks={"text": [], "tool_call": []},
    )
    assert record["status"] == OK
    assert record["problems"] == []


def test_one_bad_check_makes_the_whole_record_drift():
    record = build_record(
        base_url="", model="",
        checked_at="2026-10-20T00:00:00+00:00",
        checks={"text": [], "tool_call": ["no tool_calls in the reply"]},
    )
    assert record["status"] == DRIFT
    assert record["checks"]["text"]["status"] == OK
    assert record["checks"]["tool_call"]["status"] == DRIFT


def test_the_record_does_not_contain_a_credential():
    record = build_record(
        base_url="https://example.test/v1", model="m",
        checked_at="2026-10-20T00:00:00+00:00", checks={"text": []},
    )
    assert "key" not in repr(record).lower()


def test_verdict_is_binary_and_never_silent():
    assert verdict([]) == OK
    assert verdict(["anything"]) == DRIFT
