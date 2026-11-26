"""agent-harness-sdk: build production agents that read, analyse, decide and act, with guardrails,
human approvals and automatic OpenTelemetry MELT. DeepSeek by default; any model, any framework."""

from agent_harness_sdk.agent import STAGES, ActionResult, Agent, AgentEvent, Analysis, Decision, Observation, Run
from agent_harness_sdk.approvals import (
    ApprovalRequest,
    ApprovalResponse,
    Approver,
    AutoApprove,
    CallbackApprover,
    DenyAll,
    PendingApprovals,
)
from agent_harness_sdk.guardrails import Budget, Policy, PolicyVerdict, find_injection, redact
from agent_harness_sdk.memory import InMemoryMemory, JsonlMemory, Memory, MemoryRecord
from agent_harness_sdk.models import (
    AnthropicModel,
    ChatModel,
    DeepSeekModel,
    ModelError,
    ModelReply,
    OpenAICompatibleModel,
    ScriptedModel,
    ToolCall,
    Usage,
    model_from_env,
)
from agent_harness_sdk.telemetry import TRACE, AgentLogger, Telemetry, get_logger, setup_telemetry
from agent_harness_sdk.themis import ThemisApprover, ThemisGate, ThemisPolicy
from agent_harness_sdk.tools import Tool, ToolError, ToolRegistry, tool

__version__ = "0.1.0"
__all__ = [
    "STAGES",
    "TRACE",
    "ActionResult",
    "Agent",
    "AgentEvent",
    "AgentLogger",
    "Analysis",
    "AnthropicModel",
    "ApprovalRequest",
    "ApprovalResponse",
    "Approver",
    "AutoApprove",
    "Budget",
    "CallbackApprover",
    "ChatModel",
    "Decision",
    "DeepSeekModel",
    "DenyAll",
    "InMemoryMemory",
    "JsonlMemory",
    "Memory",
    "MemoryRecord",
    "ModelError",
    "ModelReply",
    "Observation",
    "OpenAICompatibleModel",
    "PendingApprovals",
    "Policy",
    "PolicyVerdict",
    "Run",
    "ScriptedModel",
    "Telemetry",
    "ThemisApprover",
    "ThemisGate",
    "ThemisPolicy",
    "Tool",
    "ToolCall",
    "ToolError",
    "ToolRegistry",
    "Usage",
    "find_injection",
    "get_logger",
    "model_from_env",
    "redact",
    "setup_telemetry",
    "tool",
]
