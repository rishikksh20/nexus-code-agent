from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from nexus.config.defaults import AgentConfig
from nexus.integrations.mcp import MCPServerRuntime
from nexus.memory.store import MemoryStore
from nexus.models import AgentEvent, Message, ToolExecutionContext
from nexus.prompts import build_context_sections
from nexus.context import CarryOverState, ContextBuilder, ContextCompactor, TokenEstimator, prune_tool_outputs
from nexus.runtime.context_state import load_multi_agent_state, multi_agent_carry_over_lines, render_context_packet
from nexus.runtime.execution import ExecutionMode
from nexus.hooks import HookExecutor
from nexus.runtime.sessions import SessionSnapshot, SessionStore, prepare_messages_for_model, sanitize_session_messages
from nexus.security.manager import ApprovalManager
from nexus.skills import SkillRegistry
from nexus.tools.base import ToolRegistry
from nexus.ui import TerminalUI


_PAUSED_TURN_KEY = "paused_turn"


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
    carry_over: CarryOverState = field(default_factory=CarryOverState)
    current_turn_id: str = ""
    current_trace_id: str = ""
    current_system_prompt: str = ""
    should_exit: bool = False

    @property
    def paused_turn_prompt(self) -> str:
        payload = self.session.metadata.get(_PAUSED_TURN_KEY)
        if not isinstance(payload, dict):
            return ""
        prompt = payload.get("prompt")
        return str(prompt).strip() if prompt else ""

    def has_paused_turn(self) -> bool:
        return bool(self.paused_turn_prompt)

    def mark_paused_turn(self, prompt_text: str) -> None:
        self.session.metadata[_PAUSED_TURN_KEY] = {"prompt": prompt_text}

    def clear_paused_turn(self) -> None:
        self.session.metadata.pop(_PAUSED_TURN_KEY, None)

    def consume_turn_prompt(self, raw_input: str) -> tuple[str, bool]:
        stripped = raw_input.strip()
        paused_prompt = self.paused_turn_prompt
        if not paused_prompt:
            return stripped, False
        self.clear_paused_turn()
        if _is_continue_prompt(stripped):
            return paused_prompt, True
        return stripped, False

    def build_system_prompt(self, prompt_text: str) -> str:
        sections = build_context_sections(
            self.config,
            self.tool_registry,
            task_input=prompt_text,
            execution_mode=self.mode.value,
            skill_registry=self.skill_registry,
            active_skills=self.active_skills,
            carry_over=self.carry_over,
            memory_entries=_load_all_memory(self.memory_store),
        )
        sections.carry_over.extend(multi_agent_carry_over_lines(self.session.metadata))
        self.current_system_prompt = ContextBuilder().build(sections)
        return self.current_system_prompt

    def prepare_turn(
        self,
        prompt_text: str,
        *,
        turn_id: str,
        trace_id: str,
        history: list[Message] | None = None,
        compactor_factory: Callable[[TokenEstimator, int, int], ContextCompactor] = ContextCompactor,
        estimator_factory: Callable[[], TokenEstimator] = TokenEstimator,
        prune_outputs: Callable[..., int] = prune_tool_outputs,
    ) -> PreparedTurn:
        system_prompt = self.build_system_prompt(prompt_text)
        compactor = compactor_factory(
            estimator_factory(),
            self.config.compaction_soft_limit,
            self.config.compaction_hard_limit,
        )
        source_history = self.history if history is None else history
        model_messages = prepare_messages_for_model(list(source_history))
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
                "approval_policy": self.approval_manager.policy.value,
                "allow_hidden_paths": self.config.allow_hidden_paths,
                "multi_agent_packet_summaries": {
                    packet.packet_id: render_context_packet(packet)
                    for packet in load_multi_agent_state(self.session.metadata).packets
                },
            },
        )
        return PreparedTurn(
            model_messages=model_messages,
            context=context,
            system_prompt=system_prompt,
        )

    def apply_events(self, events: list[AgentEvent]) -> None:
        usage_updates = apply_events_to_messages(self.history, events)
        for usage in usage_updates:
            _accumulate_usage(self, usage)
        self.session.messages = list(self.history)
        if not self.session.summary:
            first_user = next((message.content for message in self.history if message.role == "user"), "")
            self.session.summary = first_user
        if self.config.save_on_every_turn:
            self.session_store.save(self.session)


def apply_events_to_messages(history: list[Message], events: list[AgentEvent]) -> list:
    completed_tool_calls = {
        event.payload.call_id
        for event in events
        if event.kind == "tool_result"
    }
    usage_updates = []
    for event in events:
        if event.kind == "model_response":
            message = event.payload.message
            if message.tool_calls and not all(
                tool_call.call_id in completed_tool_calls
                for tool_call in message.tool_calls
            ):
                continue
            history.append(message)
            if event.payload.usage is not None:
                usage_updates.append(event.payload.usage)
        elif event.kind == "tool_result":
            history.append(
                Message(
                    role="tool",
                    content=event.payload.output,
                    name=event.payload.tool_name,
                    tool_call_id=event.payload.call_id,
                )
            )
    return usage_updates


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


def _is_continue_prompt(value: str) -> bool:
    normalized = value.strip().casefold().strip("`'\"")
    normalized = normalized.rstrip(".!?")
    return normalized == "continue"


def _load_all_memory(store: MemoryStore) -> list[str]:
    """Return all entries from *store* as pre-formatted ``"key: content"`` strings.

    Called once per system-prompt build so the agent always sees the full
    persistent memory regardless of which session it is in.  Multi-line values
    are preserved; the caller (ContextBuilder) wraps each item in a list bullet.
    """
    keys = store.list_keys()
    entries: list[str] = []
    for key in keys:
        entry = store.load(key)
        if entry is None:
            continue
        content = entry.content.strip()
        if content:
            entries.append(f"{key}: {content}")
    return entries
