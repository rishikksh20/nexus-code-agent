from __future__ import annotations

import sys


def resolve_prompt(params: dict) -> str | None:
    if params.get("prompt"):
        return params["prompt"]
    if params.get("prompt_file"):
        return params["prompt_file"].read_text(encoding="utf-8")
    if params.get("use_stdin"):
        return sys.stdin.read()
    return None
