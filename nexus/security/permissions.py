"""Permission evaluation for nexus tools.

``PermissionChecker`` decides whether a tool invocation should be
ALLOWED, require CONFIRMATION, or be DENIED.  The decision is driven by:

* The tool's mutating flag (:attr:`BaseTool.is_mutating`)
* The execution mode (PLAN / DEFAULT / AUTO)
* The approval policy (:class:`~nexus.security.policy.ApprovalPolicy`)
* Bash command risk — via :class:`~nexus.security.classifier.CommandClassifier`
* Path-based hard denials for write tools
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext
from nexus.runtime.execution import ExecutionMode
from nexus.security.classifier import CommandClassifier, RiskLevel
from nexus.security.policy import ApprovalPolicy
from nexus.tools.base import BaseTool


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(slots=True, frozen=True)
class PermissionResult:
    decision: PermissionDecision
    reason: str
    risk_level: str = "unknown"


class PermissionChecker:
    """Evaluate a tool invocation against the current security policy.

    Parameters
    ----------
    policy:
        The active :class:`ApprovalPolicy`.  Defaults to ``ON_REQUEST``.
    """

    def __init__(self, policy: ApprovalPolicy = ApprovalPolicy.ON_REQUEST) -> None:
        self.policy = policy

    def evaluate(
        self,
        tool: BaseTool,
        arguments: dict[str, Any],
        mode: ExecutionMode,
        *,
        context: ToolExecutionContext | None = None,
        auto_confirm_read_only: bool = True,
    ) -> PermissionResult:
        """Return a :class:`PermissionResult` for the given tool invocation."""

        # 1. Path-based hard denials (always enforced regardless of policy).
        path_policy = self._path_policy(tool, arguments, context)
        if path_policy is not None:
            return path_policy

        # 2. PLAN policy / mode — block all mutating operations.
        in_plan_mode = (
            mode is ExecutionMode.PLAN
            or self.policy is ApprovalPolicy.PLAN
        )

        # 3. Bash tool — classify command risk level explicitly.
        if tool.name == "bash":
            return self._bash_policy(arguments, mode, in_plan_mode)

        # 4. write_file — always high risk, confirm or deny.
        if tool.name == "write_file":
            if in_plan_mode:
                return PermissionResult(
                    decision=PermissionDecision.DENY,
                    reason="Mutating tools are blocked in plan mode.",
                    risk_level=RiskLevel.HIGH,
                )
            return PermissionResult(
                decision=PermissionDecision.CONFIRM,
                reason="write_file replaces the entire file — confirmation required.",
                risk_level=RiskLevel.HIGH,
            )

        # 4b. memory — get/list are read-only; set/delete/clear are mutating.
        if tool.name == "memory":
            action = str(arguments.get("action", "")).strip().lower()
            if action in {"get", "list"}:
                return PermissionResult(
                    decision=PermissionDecision.ALLOW,
                    reason="Read-only memory access is allowed.",
                    risk_level=RiskLevel.LOW,
                )
            if in_plan_mode:
                return PermissionResult(
                    decision=PermissionDecision.DENY,
                    reason="Mutating tools are blocked in plan mode.",
                    risk_level=RiskLevel.MEDIUM,
                )
            if self.policy is ApprovalPolicy.AUTO or mode is ExecutionMode.AUTO:
                return PermissionResult(
                    decision=PermissionDecision.ALLOW,
                    reason="Mutating memory update allowed under auto policy/mode.",
                    risk_level=RiskLevel.MEDIUM,
                )
            return PermissionResult(
                decision=PermissionDecision.CONFIRM,
                reason="Memory update requires confirmation.",
                risk_level=RiskLevel.MEDIUM,
            )

        # 5. Standard mutating / read-only logic for all other tools.
        if not tool.is_mutating:
            if mode is ExecutionMode.DEFAULT and not auto_confirm_read_only:
                return PermissionResult(
                    decision=PermissionDecision.CONFIRM,
                    reason="Read-only tool requires confirmation because auto_confirm_read_only is disabled.",
                    risk_level=RiskLevel.LOW,
                )
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason="Read-only tool is allowed.",
                risk_level=RiskLevel.LOW,
            )

        # Mutating tool from here on.
        if in_plan_mode:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason="Mutating tools are blocked in plan mode.",
                risk_level=RiskLevel.MEDIUM,
            )

        # AUTO policy or AUTO mode: allow mutating tools without confirmation.
        if self.policy is ApprovalPolicy.AUTO or mode is ExecutionMode.AUTO:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason="Mutating tool allowed under auto policy/mode.",
                risk_level=RiskLevel.MEDIUM,
            )

        return PermissionResult(
            decision=PermissionDecision.CONFIRM,
            reason="Mutating tool requires confirmation.",
            risk_level=RiskLevel.MEDIUM,
        )

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _bash_policy(
        self,
        arguments: dict[str, Any],
        mode: ExecutionMode,
        in_plan_mode: bool,
    ) -> PermissionResult:
        """Return a permission decision for a bash tool invocation."""
        command = str(arguments.get("command", ""))
        risk = CommandClassifier.classify(command)
        preview = command[:120].replace("\n", " ")

        if risk in {RiskLevel.HIGH, RiskLevel.DANGEROUS}:
            if in_plan_mode:
                return PermissionResult(
                    decision=PermissionDecision.DENY,
                    reason=f"{risk.value.capitalize()}-risk bash commands are blocked in plan mode.",
                    risk_level=risk,
                )
            # Always confirm high/dangerous — even in AUTO mode.
            return PermissionResult(
                decision=PermissionDecision.CONFIRM,
                reason=f"{risk.value.capitalize()}-risk bash command requires confirmation: `{preview}`",
                risk_level=risk,
            )

        if risk is RiskLevel.MEDIUM:
            if in_plan_mode:
                return PermissionResult(
                    decision=PermissionDecision.DENY,
                    reason="Medium-risk bash commands are blocked in plan mode.",
                    risk_level=risk,
                )
            # AUTO policy or AUTO mode: allow medium-risk without confirmation.
            if self.policy is ApprovalPolicy.AUTO or mode is ExecutionMode.AUTO:
                return PermissionResult(
                    decision=PermissionDecision.ALLOW,
                    reason="Medium-risk bash command allowed in auto mode.",
                    risk_level=risk,
                )
            return PermissionResult(
                decision=PermissionDecision.CONFIRM,
                reason=f"Medium-risk bash command requires confirmation: `{preview}`",
                risk_level=risk,
            )

        # Low risk: allow in all non-plan modes.
        return PermissionResult(
            decision=PermissionDecision.ALLOW,
            reason="Low-risk bash command allowed.",
            risk_level=RiskLevel.LOW,
        )

    def _path_policy(
        self,
        tool: BaseTool,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None,
    ) -> PermissionResult | None:
        _WRITE_TOOLS = {"write_file"}
        if tool.name not in _WRITE_TOOLS or context is None:
            return None

        raw_path = str(arguments.get("path", "")).strip()
        if not raw_path:
            return None

        candidate = Path(raw_path).expanduser()
        workspace_root = context.working_directory.resolve()
        target = (
            candidate.resolve()
            if candidate.is_absolute()
            else (workspace_root / candidate).resolve()
        )

        try:
            target.relative_to(workspace_root)
        except ValueError:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason="Writes outside the current workspace are denied by runtime policy.",
                risk_level=RiskLevel.DANGEROUS,
            )

        nexus_state_root = (workspace_root / ".nexus").resolve()
        nexus_memory_root = (nexus_state_root / "memory").resolve()
        try:
            target.relative_to(nexus_memory_root)
        except ValueError:
            pass
        else:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason="Direct writes into `.nexus/memory` are denied. Use the `memory` tool for persistent memory instead.",
                risk_level=RiskLevel.HIGH,
            )
        try:
            target.relative_to(nexus_state_root)
        except ValueError:
            return None
        return PermissionResult(
            decision=PermissionDecision.DENY,
            reason="Writes into Nexus-managed .nexus state are denied by runtime policy.",
            risk_level=RiskLevel.HIGH,
        )
