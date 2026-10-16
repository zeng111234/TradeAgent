"""Email text must say whether a model wrote it.

Every email-producing path in this project falls back to a hardcoded template
when the model cannot answer. That is a reasonable product decision. What is
not reasonable is returning the template with the same shape as a generated
email, on a screen whose heading says "Generated Email", for text a human then
sends to a real buyer.

``ai_powered`` was already the convention for the analysis endpoints. These
tests extend it to the two email-writing paths that were missing it, and pin
the three distinct reasons a fallback can happen.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import llm
from app.config import settings
from app.services.ai_service import generate_cold_email
from app.workflows import _generate_lead_draft_email

ROOT = Path(__file__).resolve().parent.parent.parent

LEAD = {
    "company_name": "EuroTextil GmbH",
    "country": "Germany",
    "website": "https://www.eurotextil.de",
    "snippet": "wholesale embroidery supplies",
    "emails": ["hans@eurotextil.de"],
}


class _StubClient:
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _response(content, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        ]
    )


@pytest.fixture
def configured(monkeypatch):
    """A key is present, and the provider is replaced by the stub."""
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")

    def _install(outcome):
        stub = _StubClient(outcome)
        monkeypatch.setattr(llm, "get_client", lambda: stub)
        return stub

    return _install


# --------------------------------------------------------------------------
# generate_cold_email
# --------------------------------------------------------------------------

async def test_a_model_answer_is_labelled_as_one(configured):
    configured(_response("SUBJECT: Premium thread for EuroTextil\nBODY: <p>Hello</p>"))

    result = await generate_cold_email(product_name="gold thread")

    assert result["ai_powered"] is True
    assert result["fallback_reason"] is None
    assert result["subject"] == "Premium thread for EuroTextil"
    assert result["body"] == "<p>Hello</p>"


async def test_a_truncated_answer_is_not_passed_off_as_an_email(configured):
    configured(_response("SUBJECT: Premium thread\nBODY: <p>Dear Han", finish_reason="length"))

    result = await generate_cold_email(product_name="gold thread")

    assert result["ai_powered"] is False
    assert "token cap" in result["fallback_reason"]
    # And the text really is the template, not the half-finished answer.
    assert "Dear Han" not in result["body"]


async def test_a_failed_call_says_it_failed(configured):
    configured(RuntimeError("connection reset"))

    result = await generate_cold_email(product_name="gold thread")

    assert result["ai_powered"] is False
    assert "connection reset" in result["fallback_reason"]


async def test_an_empty_answer_is_not_a_generated_email(configured):
    configured(SimpleNamespace(choices=[]))

    result = await generate_cold_email(product_name="gold thread")

    assert result["ai_powered"] is False
    assert result["fallback_reason"]


async def test_no_key_is_reported_as_no_key(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")

    result = await generate_cold_email(product_name="gold thread")

    assert result["ai_powered"] is False
    assert result["fallback_reason"] == "no_api_key"


# --------------------------------------------------------------------------
# _generate_lead_draft_email — the daily scan's draft generator
# --------------------------------------------------------------------------

async def test_an_agent_written_draft_is_labelled(configured):
    configured(_response('{"subject": "Thread supplier", "body": "Dear EuroTextil,"}'))

    draft = await _generate_lead_draft_email(LEAD, "gold thread")

    assert draft["ai_powered"] is True
    assert draft["fallback_reason"] is None
    assert draft["subject"] == "Thread supplier"
    assert draft["body"] == "Dear EuroTextil,"


async def test_a_fallback_draft_is_labelled(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")

    draft = await _generate_lead_draft_email(LEAD, "gold thread")

    assert draft["ai_powered"] is False
    assert draft["fallback_reason"] == "no_api_key"
    assert draft["body"]  # still usable, just not model output


async def test_a_truncated_draft_is_not_returned_as_partial_text(configured):
    configured(_response('{"subject": "Thread supplier", "body": "Dear Euro', finish_reason="length"))

    draft = await _generate_lead_draft_email(LEAD, "gold thread")

    assert draft["ai_powered"] is False
    assert "token cap" in draft["fallback_reason"]
    # The distinctive template wording, i.e. not the fragment the model managed.
    assert "I noticed your company in Germany" in draft["body"]
    assert draft["subject"] != "Thread supplier"


async def test_a_lead_without_an_email_is_skipped(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")

    assert await _generate_lead_draft_email({**LEAD, "emails": []}, "gold thread") is None


# --------------------------------------------------------------------------
# The reviewed-draft endpoint must actually write the drafts it offers
# --------------------------------------------------------------------------

def test_scan_leads_draft_workflow_writes_drafts_instead_of_reading_a_missing_key():
    """It used to read ``lead["draft_email"]``, a key the scanner never sets.

    So every "AI-generated draft" on that screen was the inline one-line
    template and every subject was the same hardcoded string, while the
    docstring promised AI drafts and the UI offered them to send.
    """
    path = ROOT / "backend" / "app" / "workflows.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "scan_leads_draft_workflow"),
        None,
    )
    assert fn is not None
    source = ast.unparse(fn)

    assert "_generate_lead_draft_email" in source, (
        "scan_leads_draft_workflow must generate the drafts, not look them up"
    )
    assert "lead.get('draft_email'" not in source, (
        "reading lead['draft_email'] yields nothing; the scanner does not set it"
    )


# --------------------------------------------------------------------------
# Over HTTP, because the response model can silently drop a new field
# --------------------------------------------------------------------------

def test_the_generate_endpoint_surfaces_the_fallback(client, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")

    r = client.post("/api/v1/emails/generate", json={"product_name": "gold thread"})

    assert r.status_code == 200
    body = r.json()
    assert body["ai_powered"] is False
    assert body["fallback_reason"] == "no_api_key"


def test_the_generate_endpoint_surfaces_a_real_answer(client, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    stub = _StubClient(_response("SUBJECT: Thread for you\nBODY: <p>Hi</p>"))
    monkeypatch.setattr(llm, "get_client", lambda: stub)

    r = client.post("/api/v1/emails/generate", json={"product_name": "gold thread"})

    assert r.status_code == 200
    body = r.json()
    assert body["ai_powered"] is True
    assert body["fallback_reason"] is None
    assert body["subject"] == "Thread for you"
