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
]
