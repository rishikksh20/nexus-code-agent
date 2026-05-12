from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nexus.config.defaults import AgentConfig
from nexus.integrations.mcp import MCPServerRuntime
from nexus.memory.store import MemoryStore
from nexus.models import AgentEvent, Message, ToolExecutionContext
from nexus.prompts import build_context_sections
from nexus.context import CarryOverState, ContextBuilder, ContextCompactor, TokenEstimator, prune_tool_outputs
from nexus.runtime.delegation import DelegationRuntime
from nexus.runtime.execution import ExecutionMode
from nexus.hooks import HookExecutor
from nexus.runtime.sessions import SessionSnapshot, SessionStore, sanitize_session_messages
from nexus.security.manager import ApprovalManager
from nexus.skills import SkillRegistry
from nexus.tools.base import ToolRegistry
from nexus.ui import TerminalUI


@dataclass(slots=True, frozen=True)
class PreparedTurn:
    model_messages: list[Message]
    context: ToolExecutionContext
    system_prompt: str


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

    def build_system_prompt(self, prompt_text: str) -> str:
        sections = build_context_sections(
            self.config,
            self.tool_registry,
            task_input=prompt_text,
            execution_mode=self.mode.value,
            skill_registry=self.skill_registry,
            active_skills=self.active_skills,
            carry_over=self.carry_over,
        )
        memory_matches = self.memory_store.search(prompt_text)
        if memory_matches:
            sections.project_notes.extend(entry.content for entry in memory_matches[:3])
        self.current_system_prompt = ContextBuilder().build(sections)
        return self.current_system_prompt

    def prepare_turn(
        self,
        prompt_text: str,
        *,
        turn_id: str,
        trace_id: str,
        compactor_factory: Callable[[TokenEstimator, int, int], ContextCompactor] = ContextCompactor,
        estimator_factory: Callable[[], TokenEstimator] = TokenEstimator,
        prune_outputs: Callable[..., None] = prune_tool_outputs,
    ) -> PreparedTurn:
        system_prompt = self.build_system_prompt(prompt_text)
        compactor = compactor_factory(
            estimator_factory(),
            self.config.compaction_soft_limit,
            self.config.compaction_hard_limit,
        )
        model_messages = sanitize_session_messages(list(self.history))
        if self.config.context_prune_enabled:
            prune_outputs(
                model_messages,
                protect_tokens=self.config.context_prune_protect_tokens,
                minimum_tokens=self.config.context_prune_minimum_tokens,
            )
        if compactor.should_compact(model_messages):
            model_messages, self.carry_over = compactor.compact(
                model_messages,
                self.carry_over,
                keep_recent=self.config.compaction_keep_recent,
            )
        context = ToolExecutionContext(
            session_id=self.session.session_id,
            working_directory=self.config.workspace_root,
            metadata={
                "turn_id": turn_id,
                "trace_id": trace_id,
                "active_skills": list(self.active_skills),
            },
        )
        return PreparedTurn(
            model_messages=model_messages,
            context=context,
            system_prompt=system_prompt,
        )

    def apply_events(self, events: list[AgentEvent]) -> None:
        completed_tool_calls = {
            event.payload.call_id
            for event in events
            if event.kind == "tool_result"
        }
        for event in events:
            if event.kind == "model_response":
                message = event.payload.message
                if message.tool_calls and not all(
                    tool_call.call_id in completed_tool_calls
                    for tool_call in message.tool_calls
                ):
                    continue
                self.history.append(message)
                if event.payload.usage is not None:
                    _accumulate_usage(self, event.payload.usage)
            elif event.kind == "tool_result":
                self.history.append(
                    Message(
                        role="tool",
                        content=event.payload.output,
                        name=event.payload.tool_name,
                        tool_call_id=event.payload.call_id,
                    )
                )
        self.session.messages = list(self.history)
        if not self.session.summary:
            first_user = next((message.content for message in self.history if message.role == "user"), "")
            self.session.summary = first_user
        if self.config.save_on_every_turn:
            self.session_store.save(self.session)


def _accumulate_usage(state: ReplState, usage) -> None:
    summary = state.session.metadata.setdefault(
        "usage",
        {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
        },
    )
    summary["prompt_tokens"] += usage.prompt_tokens
    summary["completion_tokens"] += usage.completion_tokens
    summary["total_tokens"] += usage.total_tokens
    summary["estimated_cost_usd"] = round(
        summary["estimated_cost_usd"] + usage.estimated_cost_usd,
        6,
    )
