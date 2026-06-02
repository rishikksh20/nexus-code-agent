from __future__ import annotations

from typing import Any

from nexus.models import AskUserOption, AskUserRequest, ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind


_YES_NO_OPTIONS = (
    AskUserOption(id="yes", label="Yes"),
    AskUserOption(id="no", label="No"),
)


class AskUserTool(Tool):
    """Pause supervisor execution and request one focused user clarification."""

    name = "ask_user"
    description = (
        "Ask the user one focused clarification question only when repository inspection and safe "
        "defaults cannot resolve an ambiguity that materially affects the result. Prefer choice options "
        "when the valid decisions are known. Do not use this for routine exploration or tool approvals."
    )
    kind = ToolKind.USER_INPUT
    is_mutating = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "One focused question for the user.",
            },
            "reason": {
                "type": "string",
                "maxLength": 500,
                "description": "Short explanation of why the answer is needed.",
            },
            "answer_type": {
                "type": "string",
                "enum": ["text", "choice", "yes_no"],
                "default": "text",
            },
            "options": {
                "type": "array",
                "minItems": 2,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": 80},
                        "label": {"type": "string", "minLength": 1, "maxLength": 120},
                        "description": {"type": "string", "maxLength": 300},
                    },
                    "required": ["id", "label"],
                    "additionalProperties": False,
                },
            },
            "default_option_id": {
                "type": "string",
                "description": "Optional recommended option id. Pressing Enter selects this option.",
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    def get_user_input_request(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> AskUserRequest:
        del call_id, context
        question = str(arguments.get("question", "")).strip()
        raw_reason = arguments.get("reason")
        reason = str(raw_reason).strip() or None if raw_reason is not None else None
        answer_type = str(arguments.get("answer_type", "text")).strip().lower() or "text"
        raw_default_option_id = arguments.get("default_option_id")
        default_option_id = (
            str(raw_default_option_id).strip() or None
            if raw_default_option_id is not None
            else None
        )

        if not question:
            raise ValueError("ask_user requires a non-empty question.")
        if len(question) > 500:
            raise ValueError("ask_user question must be 500 characters or fewer.")
        if reason and len(reason) > 500:
            raise ValueError("ask_user reason must be 500 characters or fewer.")
        if answer_type not in {"text", "choice", "yes_no"}:
            raise ValueError("ask_user answer_type must be one of: text, choice, yes_no.")

        raw_options = arguments.get("options", [])
        if not isinstance(raw_options, list):
            raise ValueError("ask_user options must be a list.")

        if answer_type == "yes_no":
            if raw_options:
                raise ValueError("ask_user yes_no options are runtime-owned; omit options.")
            if default_option_id not in {None, "no"}:
                raise ValueError("ask_user yes_no default_option_id must be omitted or set to 'no'.")
            return AskUserRequest(
                question=question,
                reason=reason,
                answer_type="yes_no",
                options=_YES_NO_OPTIONS,
                default_option_id="no",
            )

        options = tuple(_parse_option(option) for option in raw_options)
        if answer_type == "text":
            if options or default_option_id:
                raise ValueError("ask_user text questions cannot define options or a default_option_id.")
            return AskUserRequest(question=question, reason=reason)

        if not 2 <= len(options) <= 6:
            raise ValueError("ask_user choice questions require between 2 and 6 options.")
        option_ids = [option.id for option in options]
        if len(set(option_ids)) != len(option_ids):
            raise ValueError("ask_user choice option ids must be unique.")
        if default_option_id and default_option_id not in option_ids:
            raise ValueError("ask_user default_option_id must match one choice option id.")
        return AskUserRequest(
            question=question,
            reason=reason,
            answer_type="choice",
            options=options,
            default_option_id=default_option_id,
        )

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        del arguments, context
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output="Internal runtime error: ask_user must be handled as a clarification interrupt.",
            is_error=True,
        )


def _parse_option(payload: Any) -> AskUserOption:
    if not isinstance(payload, dict):
        raise ValueError("ask_user choice options must be objects.")
    option_id = str(payload.get("id", "")).strip()
    label = str(payload.get("label", "")).strip()
    raw_description = payload.get("description")
    description = str(raw_description).strip() or None if raw_description is not None else None
    if not option_id or len(option_id) > 80:
        raise ValueError("ask_user choice option ids must contain 1 to 80 characters.")
    if not label or len(label) > 120:
        raise ValueError("ask_user choice option labels must contain 1 to 120 characters.")
    if description and len(description) > 300:
        raise ValueError("ask_user choice option descriptions must be 300 characters or fewer.")
    return AskUserOption(id=option_id, label=label, description=description)
