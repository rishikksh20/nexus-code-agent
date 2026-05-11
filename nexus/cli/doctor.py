from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from nexus.runtime.sandbox import docker_available, docker_image_available
from nexus.ui import TerminalUI


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DoctorCheck:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(slots=True)
class DoctorGate:
    name: str
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def status(self) -> str:
        statuses = {check.status for check in self.checks}
        if "fail" in statuses:
            return "fail"
        if "warn" in statuses:
            return "warn"
        return "pass"

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(slots=True)
class DoctorReport:
    generated_at: str
    overall_status: str
    gates: list[DoctorGate]
    registered_tools: list[str]
    mcp_servers: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "overall_status": self.overall_status,
            "gates": [gate.to_dict() for gate in self.gates],
            "registered_tools": self.registered_tools,
            "mcp_servers": self.mcp_servers,
        }


def build_doctor_report(config, registry, resources) -> DoctorReport:
    gates = [
        DoctorGate(
            name="Runtime Integrity",
            checks=[
                _status(
                    "workspace_root",
                    config.workspace_root.exists(),
                    f"Workspace root: {config.workspace_root}",
                    fail_detail=f"Workspace root does not exist: {config.workspace_root}",
                ),
                _status(
                    "tool_registry",
                    bool(registry.records()),
                    f"Registered tools: {len(registry.records())}",
                    fail_detail="No tools were registered; the runtime cannot complete useful work.",
                ),
                _warn_if(
                    "audit_logging",
                    config.log_format != "json",
                    "Set log_format = \"json\" for attributable production logs and metrics output.",
                    pass_detail=f"Structured runtime logs enabled in {config.log_dir}",
                ),
            ],
        ),
        DoctorGate(
            name="Safety Integrity",
            checks=[
                _warn_if(
                    "default_mode",
                    config.default_mode == "auto",
                    "default_mode is auto; keep default or plan for narrower approval boundaries in production.",
                    pass_detail=f"default_mode is {config.default_mode}",
                ),
                _status(
                    "write_note_limit",
                    config.write_note_max_bytes > 0,
                    f"write_note payload limit: {config.write_note_max_bytes} bytes",
                    fail_detail="write_note_max_bytes must be greater than 0.",
                ),
                _sandbox_check(config),
            ],
        ),
        DoctorGate(
            name="Operational Integrity",
            checks=[
                _directory_writable_check("session_dir", config.session_dir),
                _directory_writable_check("memory_dir", config.memory_dir),
                _directory_writable_check("log_dir", config.log_dir),
            ],
        ),
        DoctorGate(
            name="Extension Integrity",
            checks=[
                _directory_writable_check("plugins_dir", config.plugins_dir),
                _mcp_check(config, resources),
                _status(
                    "tool_filters",
                    not (set(config.allowed_tools) & set(config.denied_tools)),
                    "Allowed and denied tool filters do not overlap.",
                    fail_detail="allowed_tools and denied_tools overlap.",
                ),
            ],
        ),
    ]
    overall_status = "pass"
    gate_statuses = {gate.status for gate in gates}
    if "fail" in gate_statuses:
        overall_status = "fail"
    elif "warn" in gate_statuses:
        overall_status = "warn"
    return DoctorReport(
        generated_at=datetime.now(UTC).isoformat(),
        overall_status=overall_status,
        gates=gates,
        registered_tools=[record.name for record in registry.records()],
        mcp_servers=[
            {
                "name": runtime.server.name,
                "registered_tools": list(runtime.registered_tools),
                "last_error": runtime.last_error,
            }
            for runtime in resources.mcp_servers
        ],
    )


def render_doctor_report(ui: TerminalUI, report: DoctorReport, *, output_format: str) -> None:
    ui.print_doctor_report(report, output_format=output_format)


def exit_code_for_report(report: DoctorReport) -> int:
    return 1 if report.overall_status == "fail" else 0


def _status(name: str, ok: bool, pass_detail: str, *, fail_detail: str) -> DoctorCheck:
    return DoctorCheck(name=name, status="pass" if ok else "fail", detail=pass_detail if ok else fail_detail)


def _warn_if(name: str, condition: bool, warn_detail: str, *, pass_detail: str) -> DoctorCheck:
    return DoctorCheck(name=name, status="warn" if condition else "pass", detail=warn_detail if condition else pass_detail)


def _sandbox_check(config) -> DoctorCheck:
    if not config.sandbox_commands:
        return DoctorCheck(
            name="sandbox",
            status="pass",
            detail="Sandboxed command execution is disabled, which is acceptable for a minimal production rollout.",
        )
    if not docker_available():
        return DoctorCheck(name="sandbox", status="fail", detail="Sandboxed commands are enabled but Docker is not available.")
    if not docker_image_available(config.sandbox_image):
        return DoctorCheck(
            name="sandbox",
            status="fail",
            detail=(
                f"Sandboxed commands are enabled but image {config.sandbox_image} is not built. "
                f"Run: docker build -f nexus/Dockerfile.sandbox -t {config.sandbox_image} ."
            ),
        )
    return DoctorCheck(name="sandbox", status="pass", detail=f"Sandbox image ready: {config.sandbox_image}")


def _directory_writable_check(name: str, path: Path) -> DoctorCheck:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".doctor-{uuid4().hex}.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return DoctorCheck(name=name, status="pass", detail=f"Writable directory: {path}")
    except OSError as exc:
        logger.warning("Doctor write probe failed for %s: %s", path, exc)
        return DoctorCheck(name=name, status="fail", detail=f"Directory is not writable: {path} ({exc})")


def _mcp_check(config, resources) -> DoctorCheck:
    if not config.mcp_servers:
        return DoctorCheck(name="mcp_servers", status="pass", detail="No MCP servers configured.")
    failed = [runtime.server.name for runtime in resources.mcp_servers if runtime.last_error]
    if failed:
        return DoctorCheck(
            name="mcp_servers",
            status="fail",
            detail="MCP connectivity failed for: " + ", ".join(sorted(failed)),
        )
    registered = [runtime.server.name for runtime in resources.mcp_servers if runtime.registered_tools]
    if not registered:
        return DoctorCheck(
            name="mcp_servers",
            status="fail",
            detail="MCP servers are configured but no MCP tools were registered.",
        )
    return DoctorCheck(
        name="mcp_servers",
        status="pass",
        detail="Connected MCP servers: " + ", ".join(sorted(registered)),
    )