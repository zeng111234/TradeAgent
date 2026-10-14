"""Tool registry: schema derivation, validation, and failure containment."""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.agent.tools import ToolRegistry, ToolResult, tool


# --- fixtures: a tiny toy toolset -----------------------------------------

@tool(description="Add two integers")
async def add(a: int, b: int) -> dict:
    return {"sum": a + b}


@tool(description="Look something up")
async def lookup(query: str, limit: int = 3) -> dict:
    return {"query": query, "limit": limit, "hits": ["x"] * limit}


@tool(description="Always explodes")
async def explode(reason: str = "boom") -> dict:
    raise RuntimeError(reason)


@tool(description="Returns None")
async def nothing() -> None:
    return None


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    for t in (add, lookup, explode, nothing):
        reg.register(t)
    return reg


# --- schema derivation -----------------------------------------------------

def test_schema_uses_function_name_and_description():
    schema = add.schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "add"
    assert fn["description"] == "Add two integers"
    assert set(fn["parameters"]["properties"]) == {"a", "b"}


def test_schema_marks_only_defaultless_params_required():
    """This is what makes optional args genuinely optional for the model."""
    params = lookup.schema()["function"]["parameters"]
    assert params["required"] == ["query"]
    assert "limit" not in params["required"]
    assert params["properties"]["limit"].get("default") == 3


def test_schema_is_json_serialisable(registry):
    """The schema is sent over the wire; it must not contain Python objects."""
    import json

    json.dumps(registry.schemas())


def test_no_param_tool_has_empty_properties():
    params = nothing.schema()["function"]["parameters"]
    assert params["properties"] == {}
    assert params.get("required", []) == []


def test_hand_written_params_model_is_accepted():
    class P(BaseModel):
        url: str
        depth: int = 1

    @tool(description="custom model", params_model=P)
    async def fetch(url: str, depth: int = 1) -> dict:
        return {"url": url, "depth": depth}

    params = fetch.schema()["function"]["parameters"]
    assert params["required"] == ["url"]
    assert set(params["properties"]) == {"url", "depth"}


# --- registry behaviour ----------------------------------------------------

def test_duplicate_registration_is_rejected(registry):
    with pytest.raises(ValueError, match="duplicate tool name"):
        registry.register(add)


def test_names_are_sorted_and_complete(registry):
    assert registry.names() == ["add", "explode", "lookup", "nothing"]
    assert len(registry) == 4
    assert "add" in registry
    assert "missing" not in registry


def test_tag_filtering():
    reg = ToolRegistry()

    @tool(description="read op", tags={"read"})
    async def reader() -> dict:
        return {}

    @tool(description="write op", tags={"write"})
    async def writer() -> dict:
        return {}

    reg.register(reader)
    reg.register(writer)
    assert [t.name for t in reg.tagged("read")] == ["reader"]
    assert reg.tagged("nope") == []


# --- invocation ------------------------------------------------------------

async def test_successful_call_returns_observation(registry):
    result, elapsed = await registry.call("add", {"a": 2, "b": 3})
    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.error is None
    assert '"sum": 5' in result.observation
    assert elapsed >= 0


async def test_defaults_are_applied(registry):
    result, _ = await registry.call("lookup", {"query": "yarn"})
    assert result.ok
    assert '"limit": 3' in result.observation


async def test_arguments_may_arrive_as_a_json_string(registry):
    result, _ = await registry.call("add", '{"a": 1, "b": 1}')
    assert result.ok
    assert '"sum": 2' in result.observation


async def test_arguments_may_arrive_empty_for_no_param_tool(registry):
    for args in ("", "{}", None):
        result, _ = await registry.call("nothing", args)
        assert result.ok, f"failed for args={args!r}: {result.error}"


async def test_malformed_json_arguments_are_recoverable(registry):
    """Real endpoints do emit truncated JSON. This must not crash the loop."""
    result, _ = await registry.call("add", '{"a": 1, "b":')
    assert result.ok is False
    assert "not valid JSON" in result.error
    assert "add" in result.error


async def test_non_object_arguments_are_rejected(registry):
    result, _ = await registry.call("add", "[1, 2]")
    assert result.ok is False
    assert "must be a JSON object" in result.error


async def test_missing_required_argument_is_rejected_with_detail(registry):
    result, _ = await registry.call("add", {"a": 1})
    assert result.ok is False
    assert "invalid arguments" in result.error
    assert "b" in result.error


async def test_wrong_type_is_rejected(registry):
    result, _ = await registry.call("add", {"a": "not-a-number", "b": 1})
    assert result.ok is False
    assert "invalid arguments" in result.error


async def test_unknown_tool_is_an_observation_not_an_exception(registry):
    """The model can recover from 'no such tool'; the loop cannot recover from a raise."""
    result, elapsed = await registry.call("teleport", {})
    assert result.ok is False
    assert "unknown tool 'teleport'" in result.error
    assert "add" in result.error          # lists what IS available
    assert elapsed == 0.0


async def test_tool_exception_is_captured(registry):
    result, _ = await registry.call("explode", {"reason": "kaboom"})
    assert result.ok is False
    assert "RuntimeError" in result.error
    assert "kaboom" in result.error


async def test_none_return_is_a_success(registry):
    result, _ = await registry.call("nothing", {})
    assert result.ok is True
    assert result.observation == "null"


# --- observation bounding --------------------------------------------------

async def test_large_observations_are_truncated():
    reg = ToolRegistry()

    @tool(description="returns a lot")
    async def big() -> dict:
        return {"blob": "z" * 20_000}

    reg.register(big)
    result, _ = await reg.call("big", {})
    assert result.ok
    assert len(result.observation) < 20_000
    assert "truncated" in result.observation
    # The marker reports the pre-truncation size so a reader knows how much
    # was withheld. We assert on the pattern, not an exact byte count.
    import re

    match = re.search(r"truncated, (\d+) chars total", result.observation)
    assert match, f"missing truncation marker in: {result.observation[-120:]}"
    assert int(match.group(1)) > 20_000


async def test_unserialisable_return_values_do_not_crash():
    reg = ToolRegistry()

    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    @tool(description="returns a non-JSON object")
    async def weird() -> dict:
        return {"obj": Weird()}

    reg.register(weird)
    result, _ = await reg.call("weird", {})
    assert result.ok
    assert "weird" in result.observation.lower()


# --- function-derived models ----------------------------------------------

def test_build_params_model_matches_signature():
    from app.agent.tools import build_params_model

    async def sample(alpha: str, beta: int = 7, gamma: list[str] | None = None) -> None:
        return None

    model = build_params_model(sample, "sample")
    schema = model.model_json_schema()
    assert set(schema["properties"]) == {"alpha", "beta", "gamma"}
    assert schema["required"] == ["alpha"]


def test_unannotated_parameter_does_not_break_model_building():
    from app.agent.tools import build_params_model

    async def loose(anything) -> None:  # noqa: ANN001
        return None

    model = build_params_model(loose, "loose")
    assert "anything" in model.model_json_schema()["properties"]
