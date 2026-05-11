from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class UserProfile:
    preferred_languages: list[str] = field(default_factory=list)
    response_style: str = "concise"
    preferred_tools: list[str] = field(default_factory=list)
    recurring_workflows: list[str] = field(default_factory=list)
    common_constraints: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = ["# User Profile", ""]
        lines.append(f"- Response style: {self.response_style}")
        lines.extend(f"- Preferred language: {item}" for item in self.preferred_languages)
        lines.extend(f"- Preferred tool: {item}" for item in self.preferred_tools)
        lines.extend(f"- Workflow: {item}" for item in self.recurring_workflows)
        lines.extend(f"- Constraint: {item}" for item in self.common_constraints)
        return "\n".join(lines).strip() + "\n"