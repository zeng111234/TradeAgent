"""Tool registry — the agent's hands.

A tool is an async Python function plus a Pydantic model describing its
arguments. From that pair we derive the OpenAI-style function schema, so the
schema can never drift from the implementation: adding a parameter to the
model is the only step needed.

The registry is the **whitelist**. A tool that is not registered is invisible
to the model and uncallable. Outbound side effects (sending mail) are
deliberately absent — see ``trade_send_boundary`` in the tests.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

# Observations are fed back into the model context, so they must be bounded.
MAX_OBSERVATION_CHARS = 4000


class ToolError(Exception):
    """Raised for a *usage* problem (unknown tool, bad arguments).

    Distinct from an exception raised *inside* a tool, which is captured as an
    observation instead so the model can react to it.
    """


def compact_json(value: Any) -> str:
    """Serialise for the model, tolerating non-JSON-native objects."""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


@dataclass
class ToolResult:
    """Outcome of one tool invocation, already shaped for the model."""

    ok: bool
    observation: str
    raw: Any = None
    error: Optional[str] = None

    @classmethod
    def success(cls, value: Any) -> "ToolResult":
        """Wrap a tool's return value, or recognise a refusal.

        A mapping carrying ``error`` is a **failure**, not a success that happens
        to mention one. The tools here signal a deliberate refusal by returning
        ``{"error": ..., "reason": ...}`` -- the depth cap, the child cap, an
        unknown customer id -- and marking those ``ok=True`` made the trace
        report that a call had worked when it had done nothing. That is the same
        lie as a template presented as generated text, one level down.
        """
        text = compact_json(value)
        truncated = len(text) > MAX_OBSERVATION_CHARS
        if truncated:
            text = text[:MAX_OBSERVATION_CHARS] + f'... [truncated, {len(text)} chars total]'
        if isinstance(value, dict) and value.get("error"):
            return cls(ok=False, observation=text, raw=value, error=str(value["error"]))
        return cls(ok=True, observation=text, raw=value)

    @classmethod
    def failure(cls, message: str) -> "ToolResult":
        return cls(ok=False, observation=compact_json({"error": message}), error=message)


@dataclass
class Tool:
    name: str
    description: str
    params_model: type[BaseModel]
    fn: Callable[..., Awaitable[Any]]
    # Tags let the loop, the UI and tests talk about classes of tools.
    tags: set[str] = field(default_factory=set)

    @cached_property
    def host_params(self) -> frozenset[str]:
        """Parameters the *host* supplies, not the model.

        A parameter whose name starts with an underscore is injected by the
        caller through ``host={...}``. Two properties matter:

        * It is absent from :meth:`schema`, so the model can neither see it nor
          forge it. A deadline the model could write is not a deadline.
        * It reaches the function body, which is how ``spawn_researcher`` learns
          how much of the task's time budget is left.
        """
        return frozenset(
            name for name in inspect.signature(self.fn).parameters
            if name.startswith("_")
        )

    def schema(self) -> dict:
        """OpenAI ``tools=[...]`` entry.

        Pydantic v2 already emits the right shape: ``required`` lists only
        fields without defaults, so optional parameters are genuinely optional.
        """
        params = self.params_model.model_json_schema()
        params.pop("title", None)
        for prop in params.get("properties", {}).values():
            prop.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }

    async def invoke(
        self, arguments: Any, *, host: Optional[dict[str, Any]] = None
    ) -> ToolResult:
        """Validate then run. Never raises for a *tool* failure."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                return ToolResult.failure(
                    f"arguments for '{self.name}' are not valid JSON: {exc}. "
                    f"Received: {arguments[:200]!r}"
                )
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return ToolResult.failure(
                f"arguments for '{self.name}' must be a JSON object, got {type(arguments).__name__}"
            )

        try:
            params = self.params_model.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult.failure(
                f"invalid arguments for '{self.name}': {exc.errors(include_url=False)}"
            )

        # Host parameters are added *after* validation, so they can never be
        # supplied, overridden or corrupted by the model's arguments.
        payload = params.model_dump()
        for key in self.host_params:
            if host is not None and key in host:
                payload[key] = host[key]

        try:
            value = self.fn(**payload)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model on purpose
            logger.warning("tool %s raised: %s", self.name, exc, exc_info=True)
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")

        if inspect.isawaitable(value):
            try:
                value = await value
            except Exception as exc:  # noqa: BLE001
                logger.warning("tool %s failed: %s", self.name, exc, exc_info=True)
                return ToolResult.failure(f"{type(exc).__name__}: {exc}")

        return ToolResult.success(value)

    def __call__(self, **kwargs: Any) -> Awaitable[Any]:
        """Call the underlying function directly, validating arguments first.

        Exists so application code and tests can invoke a tool like a normal
        function::

            await list_customers(stage="quoting")

        Prefer the registry (``registry.call(name, args)``) on the agent path,
        which returns a :class:`ToolResult` instead of raising. This entry point
        raises, because a programming error in your own code should be loud.
        """
        params = self.params_model.model_validate(kwargs)
        return self.fn(**params.model_dump())


class ToolRegistry:
    """Ordered collection of tools with duplicate protection."""

    def __init__(self, *, depth: int = 0) -> None:
        self._tools: dict[str, Tool] = {}
        # How many delegation hops deep this registry's agent is. A registry
        # handed to a sub-agent is one deeper, which is what makes maxDepth
        # enforceable rather than advisory.
        self.depth = depth

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def copy(self) -> "ToolRegistry":
        """An independent registry holding the same tools.

        Exists because a registry is not safe to share between runs. Two things
        in this package once mutated a registry the caller owned — the loop
        registering ``recall_observation``, and ``attach_delegation`` registering
        ``spawn_researcher`` — and ``spawn_researcher`` closes over the delegation
        record for one run. A second attachment to the same registry hit the
        duplicate-name guard, was silently skipped, and left that run with a
        record nothing ever wrote to, while the first run's record collected the
        second run's children.

        The ``Tool`` objects are shared, which is safe: they are immutable in
        practice (name, description, params model, function). Only the mapping is
        copied, so adding a tool to one registry cannot appear in another.
        """
        clone = ToolRegistry(depth=self.depth)
        clone._tools = dict(self._tools)
        return clone

    def add(
        self,
        name: str,
        description: str,
        params_model: type[BaseModel],
        fn: Callable[..., Awaitable[Any]],
        tags: Optional[set[str]] = None,
    ) -> Tool:
        return self.register(Tool(name, description, params_model, fn, set(tags or ())))

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(
                f"unknown tool '{name}'. Available: {', '.join(sorted(self._tools))}"
            )
        return tool

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tagged(self, tag: str) -> list[Tool]:
        return [t for t in self._tools.values() if tag in t.tags]

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    async def call(
        self, name: str, arguments: Any, *, host: Optional[dict[str, Any]] = None
    ) -> tuple[ToolResult, float]:
        """Invoke by name. Returns (result, elapsed_seconds).

        Unknown tools come back as a failed observation rather than an
        exception, because the model can recover from "that tool doesn't
        exist" but the loop cannot.

        ``host`` carries the parameters the model must not supply — today, the
        run's deadline.
        """
        try:
            tool = self.get(name)
        except ToolError as exc:
            return ToolResult.failure(str(exc)), 0.0

        started = asyncio.get_event_loop().time()
        result = await tool.invoke(arguments, host=host)
        elapsed = asyncio.get_event_loop().time() - started
        return result, elapsed


def build_params_model(fn: Callable[..., Any], name: str) -> type[BaseModel]:
    """Create a Pydantic model from a function's parameter annotations.

    Lets a tool be written as a plain annotated async function without a
    hand-written model, while still validating every call.

    Underscore-prefixed parameters are excluded: they are host-injected (see
    :attr:`Tool.host_params`), so they must not appear in the schema the model
    is shown. Excluding them here is what keeps ``spawn_researcher``'s
    ``_deadline`` invisible to the model while still reaching the body.
    """
    from pydantic import create_model

    sig = inspect.signature(fn)
    fields: dict[str, tuple[Any, Any]] = {}
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls") or param_name.startswith("_"):
            continue
        annotation = param.annotation if param.annotation is not inspect.Parameter.empty else Any
        if param.default is inspect.Parameter.empty:
            fields[param_name] = (annotation, ...)
        else:
            fields[param_name] = (annotation, param.default)
    return create_model(f"{name}_Params", **fields)


def tool(
    *,
    description: str,
    name: Optional[str] = None,
    params_model: Optional[type[BaseModel]] = None,
    tags: Optional[set[str]] = None,
):
    """Decorator marking a function as a callable agent tool.

    Usage::

        @tool(description="Score a prospect", tags={"read"})
        async def score_lead(website: str, page_text: str) -> dict:
            ...
    """

    def decorate(fn: Callable[..., Awaitable[Any]]) -> Tool:
        tool_name = name or fn.__name__
        model = params_model or build_params_model(fn, tool_name)
        return Tool(tool_name, description, model, fn, set(tags or ()))

    return decorate
