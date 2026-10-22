"""Policy tests: the agent's authority boundary.

The project's core promise is that the agent can *investigate and prepare* but
never *dispatch*. That promise is only worth anything if it is enforced in code
and tested, so these tests fail loudly if someone later registers a tool that
can send, delete or rewrite.
"""
from __future__ import annotations

import pytest

from app.agent.tools import ToolRegistry
from app.agent.trade_tools import FORBIDDEN_TOOL_PATTERNS, build_trade_registry

EXPECTED_TOOLS = [
    "add_customer_contact",
    "add_customer_note",
    "classify_prospect",
    "complete_task",
    "create_email_draft",
    "create_email_template",
    "create_task",
    # Memory tools. They touch ``agent_memories`` -- the agent's own notebook --
    # and never the book. ``forget`` is the one delete in the tree, and it is
    # allowed precisely because nothing here is a person's data: a wrong note
    # about a working preference would silently steer every later thread.
    "forget",
    "get_communication_history",
    "get_customer",
    "get_customer_assessment",
    "get_customer_signals",
    # The follow-up plan, read-only. It is how the agent reads the schedule the
    # deterministic planner produced instead of guessing who to chase.
    "get_follow_up_plan",
    "get_pipeline_stats",
    "get_quality_report",
    "inspect_website",
    "list_customers",
    "list_email_logs",
    "list_email_templates",
    "list_memories",
    "list_tasks",
    "reject_email_draft",
    "remember",
    "render_email_template",
    "reschedule_task",
    "search_prospects",
    "update_customer_stage",
    "upsert_customer",
]


@pytest.fixture
def registry() -> ToolRegistry:
    return build_trade_registry()


def test_registry_exposes_exactly_the_expected_tools(registry):
    """A new tool appearing here must be a deliberate, reviewed decision."""
    assert registry.names() == EXPECTED_TOOLS


def test_no_tool_name_suggests_an_outbound_or_destructive_action(registry):
    for name in registry.names():
        for pattern in FORBIDDEN_TOOL_PATTERNS:
            assert pattern not in name, (
                f"tool '{name}' matches forbidden pattern '{pattern}'. "
                "The agent must not be able to send, dispatch or delete."
            )


def test_the_only_write_tools_are_additive_and_never_dispatch(registry):
    """Every write tool appends something, or moves one status or date.

    None of them send, and none of them rewrite what somebody else wrote.
    ``remember`` and ``forget`` are the exception that proves it: they rewrite
    and delete, but only inside the agent's own notebook. Nothing a person saved
    is reachable through them.
    """
    writers = sorted(t.name for t in registry.tagged("write"))
    assert writers == [
        "add_customer_contact",
        "add_customer_note",
        "complete_task",
        "create_email_draft",
        "create_email_template",
        "create_task",
        "forget",
        "reject_email_draft",
        "remember",
        "reschedule_task",
        "update_customer_stage",
        "upsert_customer",
    ]


def test_write_tools_never_rewrite_what_a_person_wrote(registry):
    """Three kinds of write are allowed, and each has a single-field contract.

    A tool that *creates* says what happens to a duplicate; a tool that
    *closes or moves state* says which one field changes and that the rest is
    left alone. Checked on the descriptions rather than on names, because what
    the model is told is the contract it acts on: a tool called `update_task`
    that only appended would be fine, and a tool called `create_task` that
    overwrote would not be.
    """
    for name in ("add_customer_note", "add_customer_contact", "create_task"):
        text = registry.get(name).description.lower()
        assert any(word in text for word in ("already", "overwrite", "existing", "duplicate")), (
            f"{name} does not tell the model what happens to a duplicate"
        )
    # The state movers: each names the one field that may change.
    text = registry.get("complete_task").description.lower()
    assert "only the status" in text or "only the status changes" in text
    text = registry.get("update_customer_stage").description.lower()
    assert "only the stage" in text
    text = registry.get("reschedule_task").description.lower()
    assert "only the due date" in text
    # A withdrawal keeps the record; a reader must not mistake it for a delete.
    text = registry.get("reject_email_draft").description.lower()
    assert "kept" in text and "deleted" in text


def test_draft_tool_advertises_that_it_does_not_send(registry):
    """The model must be told, in the tool description, that nothing is sent."""
    description = registry.get("create_email_draft").description.lower()
    assert "does not send" in description or "not send" in description
    assert "human review" in description or "review" in description


def test_template_tool_advertises_that_it_only_stores(registry):
    """A template tool must not read as a send tool to a hurried model."""
    description = registry.get("create_email_template").description.lower()
    assert "does not send" in description or "not send" in description
    assert "template" in description


def test_every_tool_has_a_substantive_description(registry):
    for tool in registry.schemas():
        fn = tool["function"]
        assert fn["description"].strip(), f"{fn['name']} has no description"
        assert len(fn["description"]) > 40, f"{fn['name']} description is too thin to guide a model"


def test_every_tool_schema_is_valid_json_schema(registry):
    """Schemas go over the wire; a stray type would break the API call."""
    import json

    for entry in registry.schemas():
        params = entry["function"]["parameters"]
        assert params.get("type") == "object"
        assert isinstance(params.get("properties"), dict)
        # required, when present, must be a list of declared properties
        for name in params.get("required", []):
            assert name in params["properties"]
        json.dumps(entry)


def test_agent_module_contains_no_send_helper():
    """Guard against a send tool being added outside the registry.

    Scans the agent package for a function that would dispatch mail. Anything
    found here is a boundary breach, because the registry is the only thing the
    model can reach.
    """
    from pathlib import Path

    agent_dir = Path(__file__).resolve().parent.parent / "app" / "agent"
    offenders = []
    for path in agent_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ("aiosmtplib", "smtplib", "send_message", "send_batch_emails", "send_email("):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, f"an outbound-mail capability leaked into the agent package: {offenders}"


def test_read_only_tools_are_the_majority_and_carry_the_read_tag(registry):
    readers = registry.tagged("read")
    assert len(readers) >= 7
    # The write tools must not be mislabelled as read-only.
    assert "create_email_draft" not in [t.name for t in readers]
    assert "create_email_template" not in [t.name for t in readers]
    assert "upsert_customer" not in [t.name for t in readers]


def test_registries_are_independent_instances():
    """Two registries must not share mutable state (they are per-run)."""
    first = build_trade_registry()
    second = build_trade_registry()
    assert first is not second
    assert first.names() == second.names()
