from __future__ import annotations

from dataclasses import dataclass

from nexus.memory.store import MemoryStore
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.repl_state import ReplState
from nexus.runtime.sessions import EphemeralSessionStore, SessionStore, new_snapshot
from nexus.security.manager import ApprovalManager
from nexus.security.policy import ApprovalPolicy
from nexus.skills import SkillRegistry, get_skill_roots, load_skill_registry
from nexus.tools.subagents import register_skill_subagent_tools


@dataclass(slots=True)
class RuntimeSession:
    state: ReplState
    session_resumed: bool

    @classmethod
    def create(
        cls,
        *,
        config,
        console,
        params: dict,
        tool_registry,
        hooks,
        resources,
    ) -> "RuntimeSession":
        if params["no_session"]:
            session_store: SessionStore | EphemeralSessionStore = EphemeralSessionStore()
        else:
            session_store = SessionStore(
                config.session_dir,
                max_sessions_retained=config.max_sessions_retained,
            )

        session, session_resumed = resolve_runtime_session(
            params["session"],
            session_store,
            persist_sessions=not params["no_session"],
        )

        no_skills: bool = params["no_skills"]
        skill_registry = (
            SkillRegistry()
            if no_skills
            else load_skill_registry(*get_skill_roots(config))
        )
        builtin_active = (
            []
            if no_skills
            else (["nexus-agent"] if skill_registry.get("nexus-agent") is not None else [])
        )
        active_skills = builtin_active + [
            name
            for name in params.get("skills", ())
            if skill_registry.get(name) is not None
        ]

        register_skill_subagent_tools(
            tool_registry,
            resources.delegation,
            config,
            skill_registry,
        )

        state = ReplState(
            config=config,
            mode=(
                ExecutionMode.PLAN
                if params["deny_mutating"]
                else ExecutionMode(config.default_mode)
            ),
            session=session,
            session_store=session_store,
            tool_registry=tool_registry,
            memory_store=MemoryStore(config.memory_dir),
            console=console,
            hooks=hooks,
            approval_manager=ApprovalManager(
                policy=ApprovalPolicy(config.approval_policy)
            ),
            history=list(session.messages),
            skill_registry=skill_registry,
            active_skills=active_skills,
            mcp_servers=resources.mcp_servers,
            delegation=resources.delegation,
        )
        return cls(state=state, session_resumed=session_resumed)


def resolve_runtime_session(
    session_id: str | None,
    store: SessionStore | EphemeralSessionStore,
    *,
    persist_sessions: bool = True,
) -> tuple:
    """Return ``(snapshot, resumed: bool)`` for the active runtime session."""
    if not persist_sessions:
        return new_snapshot(), False
    if session_id is not None:
        try:
            return store.load(session_id), True
        except FileNotFoundError:
            return new_snapshot(session_id=session_id), False
    latest = store.load_latest()
    if latest is not None:
        return latest, True
    return new_snapshot(), False