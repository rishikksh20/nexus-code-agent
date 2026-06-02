from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from nexus.models import (
    AskUserAnswer,
    AskUserOption,
    AskUserRequest,
    ConfirmationKind,
    ConfirmationRequest,
    ConfirmationResponse,
    ToolCall,
    ToolResult,
)


ASK_USER_INTERACTION = "ask_user"
_METADATA_KEY = "ask_user"
_RECENT_LIMIT = 20
_TURN_LIMIT = 20


class ClarificationManager:
    """Track supervisor clarification interrupts in session metadata."""

    def __init__(self, metadata: dict[str, Any], *, turn_id: str, max_questions_per_turn: int) -> None:
        self._metadata = metadata
        self._turn_id = turn_id
        self._max_questions_per_turn = max(1, int(max_questions_per_turn))

    def create_request(
        self,
        tool_call: ToolCall,
        request: AskUserRequest,
        *,
        actor: str = "",
    ) -> ConfirmationRequest | ToolResult:
        state = self._state()
        counts = state.setdefault("turn_counts", {})
        count = int(counts.get(self._turn_id, 0))
        if count >= self._max_questions_per_turn:
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.tool_name,
                output=(
                    f"ask_user limit reached for this turn ({self._max_questions_per_turn}). "
                    "Proceed with documented safe assumptions or report the unresolved blocker."
                ),
                is_error=True,
                metadata={"ask_user_limit_exceeded": True},
            )
        counts[self._turn_id] = count + 1
        _bound_dict(counts, _TURN_LIMIT)

        record = {
            "call_id": tool_call.call_id,
            "turn_id": self._turn_id,
            "status": "pending",
            "request": request.to_dict(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        recent = state.setdefault("recent", [])
        recent.append(record)
        del recent[:-_RECENT_LIMIT]
        return ConfirmationRequest(
            kind=ConfirmationKind.CLARIFICATION,
            tool_name=tool_call.tool_name,
            prompt=request.question,
            reason=request.reason or "A user decision is required before the task can continue safely.",
            call_id=tool_call.call_id,
            payload={
                "interaction": ASK_USER_INTERACTION,
                "tool_name": tool_call.tool_name,
                "actor": actor,
                **request.to_dict(),
            },
            arguments=tool_call.arguments,
        )

    def answer_request(
        self,
        request: ConfirmationRequest,
        response: ConfirmationResponse,
    ) -> ToolResult:
        answer = AskUserAnswer(
            answer=response.clarification.strip(),
            selected_option_id=response.selected_option_id,
        )
        recent = self._state().setdefault("recent", [])
        for record in reversed(recent):
            if record.get("call_id") != request.call_id or record.get("status") != "pending":
                continue
            record["status"] = "answered"
            record["answer"] = answer.to_dict()
            record["answered_at"] = datetime.now(UTC).isoformat()
            break
        output = json.dumps(
            {
                "status": "answered",
                "question": request.prompt,
                **answer.to_dict(),
            },
            sort_keys=True,
        )
        return ToolResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            output=output,
            metadata={
                "ask_user_answer": True,
                "answer_type": str(request.payload.get("answer_type", "text")),
                "selected_option_id": answer.selected_option_id,
            },
        )

    def _state(self) -> dict[str, Any]:
        state = self._metadata.setdefault(_METADATA_KEY, {})
        if not isinstance(state, dict):
            state = {}
            self._metadata[_METADATA_KEY] = state
        return state


def is_ask_user_confirmation(request: ConfirmationRequest) -> bool:
    return (
        request.kind is ConfirmationKind.CLARIFICATION
        and request.payload.get("interaction") == ASK_USER_INTERACTION
    )


def ask_user_request_from_confirmation(request: ConfirmationRequest) -> AskUserRequest:
    raw_options = request.payload.get("options", [])
    options = tuple(
        AskUserOption(
            id=str(option.get("id", "")),
            label=str(option.get("label", "")),
            description=str(option.get("description", "")) or None,
        )
        for option in raw_options
        if isinstance(option, dict)
    )
    return AskUserRequest(
        question=request.prompt,
        reason=request.reason or None,
        answer_type=str(request.payload.get("answer_type", "text")),  # type: ignore[arg-type]
        options=options,
        default_option_id=str(request.payload.get("default_option_id") or "") or None,
        max_answer_length=int(request.payload.get("max_answer_length", 1000)),
    )


def parse_ask_user_response(
    request: ConfirmationRequest,
    raw_answer: str,
) -> tuple[ConfirmationResponse | None, str | None]:
    ask = ask_user_request_from_confirmation(request)
    answer = raw_answer.strip()
    if ask.answer_type == "text":
        if not answer:
            return None, "A non-empty answer is required."
        if len(answer) > ask.max_answer_length:
            return None, f"Answer must be {ask.max_answer_length} characters or fewer."
        return ConfirmationResponse(clarification=answer), None

    if not answer and ask.default_option_id:
        answer = ask.default_option_id
    option = _match_option(ask.options, answer)
    if option is None:
        return None, "Choose an option by number, id, or exact label."
    return ConfirmationResponse(
        clarification=option.label,
        selected_option_id=option.id,
    ), None


def ask_user_input_prompt(request: ConfirmationRequest) -> str:
    ask = ask_user_request_from_confirmation(request)
    if ask.answer_type == "text":
        return "Your answer:"
    if ask.default_option_id:
        return f"Select an option [default: {ask.default_option_id}]:"
    return "Select an option:"


def ask_user_display_lines(request: ConfirmationRequest) -> list[str]:
    ask = ask_user_request_from_confirmation(request)
    lines = [ask.question]
    if ask.reason:
        lines.extend(["", f"Why this matters: {ask.reason}"])
    if ask.options:
        lines.extend(["", "Options:"])
        for index, option in enumerate(ask.options, start=1):
            marker = " [default]" if option.id == ask.default_option_id else ""
            lines.append(f"{index}. {option.label} ({option.id}){marker}")
            if option.description:
                lines.append(f"   {option.description}")
    return lines


def headless_ask_user_payload(request: ConfirmationRequest) -> dict[str, Any]:
    ask = ask_user_request_from_confirmation(request)
    return {
        "status": "needs_input",
        "request": {
            "call_id": request.call_id,
            "question": ask.question,
            "reason": ask.reason or "",
            "answer_type": ask.answer_type,
            "options": [option.to_dict() for option in ask.options],
            "default_option_id": ask.default_option_id,
        },
    }


def _match_option(options: tuple[AskUserOption, ...], raw_answer: str) -> AskUserOption | None:
    normalized = raw_answer.strip().casefold()
    if normalized.isdigit():
        index = int(normalized) - 1
        return options[index] if 0 <= index < len(options) else None
    aliases = {"y": "yes", "n": "no"}
    normalized = aliases.get(normalized, normalized)
    for option in options:
        if normalized in {option.id.casefold(), option.label.casefold()}:
            return option
    return None


def _bound_dict(payload: dict[str, Any], limit: int) -> None:
    while len(payload) > limit:
        payload.pop(next(iter(payload)))
