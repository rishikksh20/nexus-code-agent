from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from nexus.config.defaults import AgentConfig
from nexus.tools.mcp import MCPServerRuntime
from nexus.memory.store import MemoryStore
from nexus.models import AgentEvent, Message, ToolExecutionContext
from nexus.prompts import build_context_sections
from nexus.context import CarryOverState, ContextBuilder, ContextCompactor, TokenEstimator, prune_tool_outputs
from nexus.hooks import HookEvent
from nexus.runtime.context_state import load_multi_agent_state, multi_agent_carry_over_lines, render_context_packet
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.agent_scope import (
    normalize_subagent_name,
    skill_metadata_catalog,
    subagent_tool_names,
    supervisor_skill_names,
    supervisor_tool_names,
)
from nexus.hooks import HookExecutor
from nexus.runtime.sessions import SessionSnapshot, SessionStore, prepare_messages_for_model, sanitize_session_messages
from nexus.security.manager import ApprovalManager
from nexus.skills import SkillRegistry
from nexus.tools.base import ToolRegistry
from nexus.ui import TerminalUI


_PAUSED_TURN_KEY = "paused_turn"
_PAUSED_TURN_PROGRESS_LIMIT = 12
_PAUSED_TURN_TEXT_LIMIT = 500


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
    run_skills: list[str] = field(default_factory=list)
    mcp_servers: list[MCPServerRuntime] = field(default_factory=list)
    carry_over: CarryOverState = field(default_factory=CarryOverState)
    current_turn_id: str = ""
    current_trace_id: str = ""
    current_system_prompt: str = ""
    current_system_prompt_task_input: str = ""
    should_exit: bool = False
    current_turn_task: asyncio.Task | None = None
    abort_requested: bool = False
    model_client_reloader: Callable[[AgentConfig], None] | None = None
    provider_settings_opener: Callable[[], None] | None = None

    def begin_running_turn(self, task: asyncio.Task | None = None) -> None:
        self.current_turn_task = task or asyncio.current_task()
        self.abort_requested = False

    def clear_running_turn(self) -> None:
        self.current_turn_task = None
        self.abort_requested = False

    def request_abort_current_turn(self) -> bool:
        self.abort_requested = True
        task = self.current_turn_task
        if task is None or task.done():
            return False
        task.cancel()
        return True

    @property
    def paused_turn_prompt(self) -> str:
        payload = self.paused_turn_payload
        prompt = payload.get("prompt")
        return str(prompt).strip() if prompt else ""

    @property
    def paused_turn_payload(self) -> dict[str, Any]:
        payload = self.session.metadata.get(_PAUSED_TURN_KEY)
        return payload if isinstance(payload, dict) else {}

    def has_paused_turn(self) -> bool:
        return bool(self.paused_turn_prompt)

    def mark_paused_turn(
        self,
        prompt_text: str,
        *,
        reason: str = "",
        progress: list[str] | tuple[str, ...] = (),
    ) -> None:
        payload: dict[str, Any] = {"prompt": str(prompt_text or "").strip()}
        if reason:
            payload["reason"] = str(reason).strip()
        compact_progress = _compact_paused_turn_progress(progress)
        if compact_progress:
            payload["progress"] = compact_progress
        self.session.metadata[_PAUSED_TURN_KEY] = payload

    def clear_paused_turn(self) -> None:
        self.session.metadata.pop(_PAUSED_TURN_KEY, None)

    def consume_turn_prompt(self, raw_input: str) -> tuple[str, bool]:
        stripped = raw_input.strip()
        paused_prompt = self.paused_turn_prompt
        if not paused_prompt:
            if _is_continue_prompt(stripped):
                previous_prompt = _previous_user_task_prompt(self.history)
                if previous_prompt:
                    return previous_prompt, False
            return stripped, False
        paused_payload = self.paused_turn_payload
        self.clear_paused_turn()
        if _is_continue_prompt(stripped):
            return _render_paused_turn_prompt(paused_payload), True
        return stripped, False

    def build_system_prompt(self, prompt_text: str) -> str:
        self.current_system_prompt_task_input = prompt_text
        scoped_active_skills = supervisor_skill_names(self.config, self.active_skills)
        scoped_tool_registry = _supervisor_prompt_tool_registry(self.config, self.tool_registry)
        sections = build_context_sections(
            self.config,
            scoped_tool_registry,
            task_input=prompt_text,
            execution_mode=self.mode.value,
            skill_registry=self.skill_registry,
            active_skills=scoped_active_skills,
            carry_over=self.carry_over,
            memory_entries=_load_all_memory(self.memory_store),
        )
        sections.carry_over.extend(multi_agent_carry_over_lines(self.session.metadata))
        self.current_system_prompt = ContextBuilder().build(sections)
        return self.current_system_prompt

    def refresh_system_prompt(self) -> str:
        """Rebuild the cached prompt after live tool/config changes."""
        prompt_text = self.current_system_prompt_task_input or self.paused_turn_prompt
        return self.build_system_prompt(prompt_text)

    def reload_model_client(self) -> None:
        """Rebuild the live model client after provider config changes."""
        if self.model_client_reloader is not None:
            self.model_client_reloader(self.config)

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
        messages_before_prune = len(model_messages)
        if self.config.context_prune_enabled:
            pruned_tool_results = prune_outputs(
                model_messages,
                protect_tokens=self.config.context_prune_protect_tokens,
                minimum_tokens=self.config.context_prune_minimum_tokens,
            )
        else:
            pruned_tool_results = 0
        messages_before_compaction = len(model_messages)
        compacted = False
        if compactor.should_compact(model_messages):
            model_messages, self.carry_over = compactor.compact(
                model_messages,
                self.carry_over,
                keep_recent=self.config.compaction_keep_recent,
            )
            compacted = True
        context = ToolExecutionContext(
            session_id=self.session.session_id,
            working_directory=self.config.workspace_root,
            metadata={
                "turn_id": turn_id,
                "trace_id": trace_id,
                "supervisor_task_input": prompt_text,
                "session_metadata": self.session.metadata,
                "active_skills": supervisor_skill_names(self.config, self.active_skills),
                "global_active_skills": list(self.active_skills),
                "skill_catalog": skill_metadata_catalog(self.skill_registry),
                "config": self.config,
                "approval_policy": self.approval_manager.policy.value,
                "allow_hidden_paths": self.config.allow_hidden_paths,
                "supervisor_available_tools": sorted(supervisor_tool_names(self.config, self.tool_registry)),
                "multi_agent_packet_summaries": {
                    packet.packet_id: render_context_packet(packet)
                    for packet in load_multi_agent_state(self.session.metadata).packets
                },
                "context_compaction": {
                    "session_id": self.session.session_id,
                    "turn_id": turn_id,
                    "trace_id": trace_id,
                    "messages_before_prune": messages_before_prune,
                    "messages_before_compaction": messages_before_compaction,
                    "messages_after": len(model_messages),
                    "pruned_tool_results": pruned_tool_results,
                    "compacted": compacted,
                    "carry_over_entries": _carry_over_entry_count(self.carry_over),
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
        _bound_durable_tool_outputs(
            self.history,
            max_chars=self.config.tool_output_max_chars,
            protect_tokens=self.config.context_prune_protect_tokens,
            minimum_tokens=self.config.context_prune_minimum_tokens,
        )
        self.session.messages = list(self.history)
        if not self.session.summary:
            first_user = next((message.content for message in self.history if message.role == "user"), "")
            self.session.summary = first_user
        if self.config.save_on_every_turn:
            self.session_store.save(self.session)


def _supervisor_prompt_tool_registry(config: AgentConfig, registry: ToolRegistry) -> ToolRegistry:
    available = supervisor_tool_names(config, registry)
    scoped = ToolRegistry()
    for record in registry.records():
        if record.name in available:
            scoped.register(
                _prompt_scoped_tool(config, registry, record),
                source=record.source,
                origin=record.origin,
            )
    return scoped


def _prompt_scoped_tool(config: AgentConfig, registry: ToolRegistry, record) -> object:
    if not str(record.name).startswith("subagent_"):
        return record.tool
    tool = copy.copy(record.tool)
    definition = getattr(tool, "_definition", None)
    subagent_name = getattr(definition, "name", None) or normalize_subagent_name(str(record.name))
    effective_tools = subagent_tool_names(
        config,
        registry,
        str(subagent_name),
        base_allowed_tools=getattr(definition, "allowed_tools", None),
        base_allowed_mcps=getattr(definition, "allowed_mcps", None),
    )
    ordered_tool_names = tuple(
        item.name
        for item in registry.records()
        if item.name in effective_tools
        and item.name != record.name
        and not item.name.startswith("subagent_")
        and item.name != "delegate_task"
    )
    allowed_mcp_servers = tuple(
        sorted(
            {
                item.origin
                for item in registry.records()
                if item.name in effective_tools and item.source == "mcp" and item.origin
            }
        )
    )
    setattr(tool, "_prompt_allowed_tools", ordered_tool_names)
    setattr(tool, "_prompt_allowed_mcps", allowed_mcp_servers)
    return tool


def _carry_over_entry_count(carry_over: CarryOverState) -> int:
    return (
        len(carry_over.pinned_facts)
        + len(carry_over.summarized_history)
        + len(carry_over.active_constraints)
    )


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


def _render_paused_turn_prompt(payload: dict[str, Any]) -> str:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return ""
    progress = _compact_paused_turn_progress(payload.get("progress", ()))
    reason = str(payload.get("reason") or "").strip()
    if not progress and not reason:
        return prompt

    lines = [
        "Continue the interrupted Nexus task below. Use the progress notes as context, avoid repeating completed work, and take the next necessary action.",
        "",
        "Original user request:",
        prompt,
    ]
    if reason:
        lines.extend(["", f"Pause reason: {reason}"])
    if progress:
        lines.extend(["", "Progress before the pause:"])
        lines.extend(f"- {item}" for item in progress)
    return "\n".join(lines)


def _compact_paused_turn_progress(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    progress: list[str] = []
    for item in value:
        text = " ".join(str(item or "").split())
        if not text:
            continue
        progress.append(text[:_PAUSED_TURN_TEXT_LIMIT])
        if len(progress) >= _PAUSED_TURN_PROGRESS_LIMIT:
            break
    return progress


def _previous_user_task_prompt(history: list[Message]) -> str:
    for message in reversed(history):
        if message.role != "user":
            continue
        content = message.content.strip()
        if not content or content.startswith("/") or _is_continue_prompt(content):
            continue
        return content
    return ""


def _load_all_memory(store: MemoryStore) -> list[str]:
    """Return all entries from *store* as pre-formatted ``"key: content"`` strings.

    Called once per system-prompt build so the agent always sees the full
    persistent memory regardless of which session it is in.  Multi-line values
    are preserved; the caller (ContextBuilder) wraps each item in a list bullet.
    """
    entries: list[str] = []
    for entry in store.load_all():
        content = entry.content.strip()
        if content:
            entries.append(f"{entry.key}: {content}")
    return entries


def _bound_durable_tool_outputs(
    history: list[Message],
    *,
    max_chars: int,
    protect_tokens: int,
    minimum_tokens: int,
) -> None:
    if max_chars > 0:
        for index, message in enumerate(history):
            if message.role != "tool" or len(message.content) <= max_chars:
                continue
            suffix = message.content[-max_chars:]
            history[index] = Message(
                role=message.role,
                content=(
                    f"[Tool output truncated for durable history to last {max_chars} chars]\n"
                    f"{suffix}"
                ),
                name=message.name,
                reasoning_content=message.reasoning_content,
                provider_state=message.provider_state,
                tool_calls=message.tool_calls,
                tool_call_id=message.tool_call_id,
            )
    prune_tool_outputs(
        history,
        protect_tokens=protect_tokens,
        minimum_tokens=minimum_tokens,
    )
