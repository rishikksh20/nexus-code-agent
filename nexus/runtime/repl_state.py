from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nexus.config.defaults import AgentConfig
from nexus.integrations.mcp import MCPServerRuntime
from nexus.memory.store import MemoryStore
from nexus.models import Message
from nexus.runtime.context import CarryOverState
from nexus.runtime.delegation import DelegationRuntime
from nexus.runtime.execution import ExecutionMode
from nexus.hooks import HookExecutor
from nexus.runtime.sessions import SessionSnapshot, SessionStore
from nexus.security.manager import ApprovalManager
from nexus.skills import SkillRegistry
from nexus.tools.base import ToolRegistry
from nexus.ui import TerminalUI


@dataclass(slots=True)
class ReplState:
    config: AgentConfig
    mode: ExecutionMode
    session: SessionSnapshot
    session_store: SessionStore
    tool_registry: ToolRegistry
    memory_store: MemoryStore
    console: TerminalUI
    hooks: HookExecutor | None = None
    approval_manager: ApprovalManager = field(default_factory=ApprovalManager)
    history: list[Message] = field(default_factory=list)
    skill_registry: SkillRegistry = field(default_factory=SkillRegistry)
    active_skills: list[str] = field(default_factory=list)
    disabled_tools: set[str] = field(default_factory=set)
    mcp_servers: list[MCPServerRuntime] = field(default_factory=list)
    delegation: DelegationRuntime | None = None
    carry_over: CarryOverState = field(default_factory=CarryOverState)
    current_turn_id: str = ""
    current_trace_id: str = ""
    current_system_prompt: str = ""
    should_exit: bool = False
