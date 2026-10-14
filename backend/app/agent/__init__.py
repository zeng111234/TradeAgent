"""Agent layer: a bounded tool-calling loop plus its tool registry.

Public surface::

    from app.agent import AgentLoop, AgentBudget, ToolRegistry, tool, run_agent

See ``docs/agent-design.md`` for the reasoning behind the design.
"""
from .loop import (  # noqa: F401
    AgentBudget,
    AgentError,
    AgentLoop,
    AgentRun,
    TraceStep,
    ToolInvocation,
    run_agent,
    DEFAULT_SYSTEM_PROMPT,
)
from .protocol import (  # noqa: F401
    LLMClient,
    LLMReply,
    Message,
    OpenAICompatibleClient,
    ToolCall,
)
from .tools import (  # noqa: F401
    Tool,
    ToolError,
    ToolRegistry,
    ToolResult,
    build_params_model,
    tool,
)
from .context import (  # noqa: F401
    CompactionReport,
    SpillStore,
    build_recall_tool,
    compact_messages,
    estimate_tokens,
)
from .subagent import (  # noqa: F401
    CHILD_BUDGET,
    Delegation,
    DelegationRecord,
    attach_delegation,
    build_child_registry,
)

__all__ = [
    "AgentBudget",
    "AgentError",
    "AgentLoop",
    "AgentRun",
    "TraceStep",
    "ToolInvocation",
    "run_agent",
    "DEFAULT_SYSTEM_PROMPT",
    "LLMClient",
    "LLMReply",
    "Message",
    "OpenAICompatibleClient",
    "ToolCall",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "build_params_model",
    "tool",
    "CompactionReport",
    "SpillStore",
    "build_recall_tool",
    "compact_messages",
    "estimate_tokens",
    "CHILD_BUDGET",
    "Delegation",
    "DelegationRecord",
    "attach_delegation",
    "build_child_registry",
]
