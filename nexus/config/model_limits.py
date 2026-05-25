"""Known context-window limits for popular models.

Sizes are in tokens (input context window, not max output).
Used to auto-tune compaction thresholds when the user has not explicitly
overridden them.
"""
from __future__ import annotations

# Model name → context window in tokens.
# Entries are matched first by exact name, then by prefix.
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    # --- Mistral ---
    "mistral-tiny": 32_768,
    "mistral-small": 32_768,
    "mistral-small-latest": 32_768,
    "mistral-small-2402": 32_768,
    "mistral-small-2501": 32_768,
    "mistral-medium": 32_768,
    "mistral-medium-latest": 32_768,
    "mistral-large": 131_072,
    "mistral-large-latest": 131_072,
    "mistral-large-2402": 32_768,
    "mistral-large-2407": 131_072,
    "mistral-large-2411": 131_072,
    "open-mistral-7b": 32_768,
    "open-mistral-nemo": 131_072,
    "open-mixtral-8x7b": 32_768,
    "open-mixtral-8x22b": 65_536,
    "codestral-latest": 32_768,
    "codestral-2405": 32_768,
    "pixtral-large-latest": 131_072,
    # --- OpenAI ---
    "gpt-3.5-turbo": 16_385,
    "gpt-3.5-turbo-0125": 16_385,
    "gpt-4": 8_192,
    "gpt-4-0613": 8_192,
    "gpt-4-32k": 32_768,
    "gpt-4-turbo": 128_000,
    "gpt-4-turbo-preview": 128_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4o-2024-05-13": 128_000,
    "gpt-4o-2024-08-06": 128_000,
    "o1": 200_000,
    "o1-preview": 128_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    # --- Gemini ---
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-flash-lite": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.0-flash-lite": 1_048_576,
    # --- Fake / development ---
    "fake-model": 8_192,
}

_DEFAULT_CONTEXT_LIMIT = 32_768


def get_model_context_limit(model_name: str) -> int:
    """Return the known context window size for *model_name*, or a safe default.

    Matching order:
    1. Exact name lookup.
    2. Prefix lookup (e.g. ``"mistral-large-2407"`` matches ``"mistral-large"``).
    3. Fall back to ``_DEFAULT_CONTEXT_LIMIT`` (32 768 tokens).
    """
    if model_name in MODEL_CONTEXT_LIMITS:
        return MODEL_CONTEXT_LIMITS[model_name]
    # Prefix match — longest matching prefix wins
    best_len = 0
    best_limit = _DEFAULT_CONTEXT_LIMIT
    for key, limit in MODEL_CONTEXT_LIMITS.items():
        if model_name.startswith(key) and len(key) > best_len:
            best_len = len(key)
            best_limit = limit
    return best_limit
