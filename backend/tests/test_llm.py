"""The provider seam: one client, one timeout, one truncation check.

Three things are asserted here because three things were wrong before:

* ten call sites built their own ``AsyncOpenAI``, so no timeout was ever set
  and a hung provider held a request for the SDK default of 600 seconds;
* none of them looked at ``finish_reason``, so an answer cut off at
  ``max_tokens`` was parsed and stored as if it were complete;
* seven copies of the code-fence stripping had drifted apart.

The AST guard is the part that keeps this true under future edits, in the same
style as ``test_no_handrolled_scoring.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import llm
from app.config import settings

ROOT = Path(__file__).resolve().parent.parent.parent

#: Files allowed to construct a provider client. ``llm.py`` is the seam for
#: everything outside the agent runtime; ``protocol.py`` is the runtime's own
#: adapter, which keeps an injectable client so the loop can be driven offline.
SEAM_FILES = {
    "backend/app/llm.py",
    "backend/app/agent/protocol.py",
}

#: ``agent/service.py`` reads the settings because it configures the runtime's
#: injectable adapter (and raises AgentUnavailable when there is no key). It is
#: part of the runtime's own seam, so it is exempt from the settings check but
#: not from the client-construction check above.
#:
#: ``scripts/contract_canary.py`` is the scheduled probe of the same seam: it
#: builds one real client and asks three shape questions. Reading the key there
#: is the point of the script, not a bypass of ``is_configured`` -- it calls that
#: first and exits without a credential.
SETTINGS_ALLOWED_FILES = SEAM_FILES | {
    "backend/app/agent/service.py",
    "scripts/contract_canary.py",
}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for base in (ROOT / "backend" / "app", ROOT / "scripts"):
        files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(files)


def _relpath(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


# --------------------------------------------------------------------------
# Static guard: no new bypasses
# --------------------------------------------------------------------------

def test_no_module_outside_the_seam_builds_a_provider_client():
    offenders: list[str] = []
    for path in _python_files():
        if _relpath(path) in SEAM_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            text = ast.unparse(node.func)
            if text.endswith("AsyncOpenAI"):
                offenders.append(f"{_relpath(path)}:{node.lineno} {text}(...)")
    assert not offenders, (
        "a provider client was constructed outside the seam; go through "
        "app.llm (or app.agent.protocol for the runtime) so the timeout and the "
        "finish_reason check apply:\n  " + "\n  ".join(offenders)
    )


def test_no_module_outside_the_seam_calls_chat_completions_directly():
    offenders: list[str] = []
    for path in _python_files():
        if _relpath(path) in SEAM_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(
                "chat.completions.create"
            ):
                offenders.append(f"{_relpath(path)}:{node.lineno}")
    assert not offenders, (
        "chat.completions.create called outside the seam:\n  " + "\n  ".join(offenders)
    )


def test_settings_are_not_read_directly_to_decide_whether_calling_is_possible():
    """``if settings.OPENAI_API_KEY:`` bypasses the seam's own definition."""
    offenders: list[str] = []
    for path in _python_files():
        if _relpath(path) in SETTINGS_ALLOWED_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and ast.unparse(node) == "settings.OPENAI_API_KEY":
                offenders.append(f"{_relpath(path)}:{node.lineno}")
    assert not offenders, (
        "read the key through app.llm.is_configured() instead, so there is one "
        "definition of 'configured':\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# Truncation is an error, not partial text
# --------------------------------------------------------------------------

def _response(content, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        ]
    )


@pytest.mark.parametrize("reason", ["length", "max_tokens"])
def test_a_truncated_answer_raises_rather_than_returning_half_of_it(reason):
    with pytest.raises(llm.LLMTruncated):
        llm.text_of(_response("Dear Sir, I am writing to you regar", finish_reason=reason))


def test_a_complete_answer_is_returned():
    assert llm.text_of(_response("hello")) == "hello"


def test_no_choices_is_a_bad_response():
    with pytest.raises(llm.LLMBadResponse):
        llm.text_of(SimpleNamespace(choices=[]))


def test_empty_content_is_a_bad_response():
    with pytest.raises(llm.LLMBadResponse):
        llm.text_of(_response("   "))


def test_none_content_is_a_bad_response_not_an_attribute_error():
    with pytest.raises(llm.LLMBadResponse):
        llm.text_of(_response(None))


def test_content_that_is_not_text_names_the_shape_change():
    """A provider that switches to content parts must not look like our bug.

    ``.strip()`` on a list raises AttributeError, and an AttributeError here
    reads as "this application is broken" -- the opposite of what it is. The
    error has to say which shape arrived, because that is the only thing a
    reader can act on when a provider changes its contract.
    """
    with pytest.raises(llm.LLMBadResponse) as caught:
        llm.text_of(_response([{"type": "text", "text": "hello"}]))

    assert "list" in str(caught.value)
    assert "shape may have changed" in str(caught.value)


# --------------------------------------------------------------------------
# JSON parsing / fence stripping
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('  ```json{"a": 1}```  ', {"a": 1}),
    ],
)
def test_parse_json_object_tolerates_a_code_fence(raw, expected):
    assert llm.parse_json_object(raw) == expected


def test_strip_code_fence_removes_the_closing_fence():
    """The old copies used split("```")[1], which kept everything after it."""
    assert llm.strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert llm.strip_code_fence('```json\n{"a": 1}\n```\ntrailing prose') == '{"a": 1}'


def test_unparseable_json_is_a_bad_response():
    with pytest.raises(llm.LLMBadResponse):
        llm.parse_json_object("I'm afraid I can't do that")


# --------------------------------------------------------------------------
# Configuration, timeout, caching
# --------------------------------------------------------------------------

def test_not_configured_by_default_in_tests():
    assert llm.is_configured() is False


def test_get_client_refuses_without_a_key(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    llm.reset_client()
    with pytest.raises(llm.LLMUnavailable):
        llm.get_client()


def test_default_timeout_is_not_the_sdk_default():
    """600s (the SDK default) is what made a hung provider hold a request.

    The value now lives in ``app/timeouts.py`` with the rest of the ladder, and it
    is the *per attempt* number: it has to be shorter than the total a caller
    experiences (``LLM_TOTAL_SECONDS``), because the SDK retries inside one
    logical call and an attempt allowed more time than the total could never
    finish.
    """
    from app import timeouts

    assert llm.DEFAULT_TIMEOUT_SECONDS == timeouts.LLM_REQUEST_SECONDS
    assert llm.DEFAULT_TIMEOUT_SECONDS < timeouts.LLM_TOTAL_SECONDS
    assert llm.DEFAULT_TIMEOUT_SECONDS < 600.0


def test_timeout_falls_back_when_the_setting_is_nonsense(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_TIMEOUT_SECONDS", 0)
    assert llm.timeout_seconds() == llm.DEFAULT_TIMEOUT_SECONDS
    monkeypatch.setattr(settings, "OPENAI_TIMEOUT_SECONDS", 30.0)
    assert llm.timeout_seconds() == 30.0


def test_the_client_is_built_with_the_configured_timeout(monkeypatch):
    created: list[dict] = []

    class _Recorder:
        def __init__(self, **kwargs):
            created.append(kwargs)

    import openai

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "OPENAI_TIMEOUT_SECONDS", 42.0)
    monkeypatch.setattr(openai, "AsyncOpenAI", _Recorder)
    llm.reset_client()
    try:
        llm.get_client()
    finally:
        llm.reset_client()

    assert created and created[0]["timeout"] == 42.0


def test_the_client_is_reused_across_calls(monkeypatch):
    import openai

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: object())
    llm.reset_client()
    try:
        assert llm.get_client() is llm.get_client()
    finally:
        llm.reset_client()


# --------------------------------------------------------------------------
# complete_text / complete_json end to end against a stubbed client
# --------------------------------------------------------------------------

class _StubClient:
    def __init__(self, outcome):
        self.calls: list[dict] = []
        self._outcome = outcome
        completions = SimpleNamespace(create=self._create)
        self.chat = SimpleNamespace(completions=completions)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


async def test_complete_text_sends_the_system_prompt_and_the_budget(monkeypatch):
    stub = _StubClient(_response("hi"))
    monkeypatch.setattr(llm, "get_client", lambda: stub)

    text = await llm.complete_text(
        "user text", system="be terse", temperature=0.7, max_tokens=321
    )

    assert text == "hi"
    sent = stub.calls[0]
    assert sent["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "user text"},
    ]
    assert sent["temperature"] == 0.7
    assert sent["max_tokens"] == 321


async def test_complete_text_omits_the_system_message_when_there_is_none(monkeypatch):
    stub = _StubClient(_response("hi"))
    monkeypatch.setattr(llm, "get_client", lambda: stub)

    await llm.complete_text("just the user turn")
    assert [m["role"] for m in stub.calls[0]["messages"]] == ["user"]


async def test_complete_json_propagates_truncation(monkeypatch):
    stub = _StubClient(_response('{"partial": ', finish_reason="length"))
    monkeypatch.setattr(llm, "get_client", lambda: stub)

    with pytest.raises(llm.LLMTruncated):
        await llm.complete_json("give me json")


async def test_complete_json_parses_a_fenced_answer(monkeypatch):
    stub = _StubClient(_response('```json\n{"ok": true}\n```'))
    monkeypatch.setattr(llm, "get_client", lambda: stub)

    assert await llm.complete_json("give me json") == {"ok": True}
