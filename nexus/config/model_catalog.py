from __future__ import annotations

from dataclasses import dataclass


DEFAULT_BUILTIN_OUTPUT_TOKENS = 32_000


@dataclass(slots=True, frozen=True)
class BuiltinModel:
    provider: str
    model_name: str
    context_length: int
    display_name: str = ""
    openai_compatible: bool = False
    base_url: str = ""
    base_url_env: str = ""
    api_key_env: str = ""
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_reasoning: bool = False
    thinking_mode: str = "provider_default"
    thinking_budget_tokens: int | None = None
    reasoning_effort: str = ""
    max_output_tokens: int = DEFAULT_BUILTIN_OUTPUT_TOKENS
    reserved_output_tokens: int = DEFAULT_BUILTIN_OUTPUT_TOKENS
    temperature: float = 0.0
    top_p: float = 1.0


BUILTIN_MODELS: tuple[BuiltinModel, ...] = (
    BuiltinModel(
        provider="openai-compatible",
        model_name="big-pickle",
        context_length=200_000,
        display_name="Big Pickle",
        openai_compatible=True,
        base_url="https://opencode.ai/zen/v1",
        base_url_env="BASE_URL",
        api_key_env="API_KEY",
        supports_reasoning=True,
        thinking_mode="budget_tokens",
        thinking_budget_tokens=4096,
    ),
    BuiltinModel(
        provider="openai-compatible",
        model_name="mistral-medium-latest",
        context_length=32_768,
        display_name="Mistral Medium",
        openai_compatible=True,
        base_url="https://api.mistral.ai/v1",
        base_url_env="BASE_URL",
        api_key_env="MISTRAL_API_KEY",
    ),
    BuiltinModel(
        provider="mistral",
        model_name="mistral-large-latest",
        context_length=131_072,
        display_name="Mistral Large",
        openai_compatible=True,
        base_url="https://api.mistral.ai/v1",
        base_url_env="MISTRAL_BASE_URL",
        api_key_env="MISTRAL_API_KEY",
        supports_reasoning=True,
        thinking_mode="reasoning_effort",
        reasoning_effort="high",
    ),
    BuiltinModel(
        provider="openai",
        model_name="gpt-4o",
        context_length=128_000,
        display_name="GPT-4o",
        openai_compatible=True,
        base_url="https://api.openai.com/v1",
        base_url_env="BASE_URL",
        api_key_env="OPENAI_API_KEY",
        supports_reasoning=True,
        thinking_mode="reasoning_effort",
        reasoning_effort="high",
    ),
    BuiltinModel(
        provider="openai",
        model_name="gpt-4o-mini",
        context_length=128_000,
        display_name="GPT-4o Mini",
        openai_compatible=True,
        base_url="https://api.openai.com/v1",
        base_url_env="BASE_URL",
        api_key_env="OPENAI_API_KEY",
        supports_reasoning=True,
        thinking_mode="reasoning_effort",
        reasoning_effort="high",
    ),
    BuiltinModel(
        provider="gemini",
        model_name="gemini-2.5-pro",
        context_length=1_048_576,
        display_name="Gemini 2.5 Pro",
        api_key_env="GEMINI_API_KEY",
        supports_reasoning=True,
        thinking_mode="budget_tokens",
        thinking_budget_tokens=1024,
    ),
    BuiltinModel(
        provider="gemini",
        model_name="gemini-2.5-flash",
        context_length=1_048_576,
        display_name="Gemini 2.5 Flash",
        api_key_env="GEMINI_API_KEY",
        supports_reasoning=True,
        thinking_mode="budget_tokens",
        thinking_budget_tokens=1024,
    ),
    BuiltinModel(
        provider="anthropic",
        model_name="claude-sonnet-4-5",
        context_length=200_000,
        display_name="Claude Sonnet",
        api_key_env="ANTHROPIC_API_KEY",
        supports_reasoning=True,
        thinking_mode="budget_tokens",
        thinking_budget_tokens=1024,
    ),
    BuiltinModel(
        provider="ollama",
        model_name="qwen2.5-coder:7b",
        context_length=32_768,
        display_name="Qwen2.5 Coder 7B",
        openai_compatible=False,
        base_url="http://localhost:11434",
        base_url_env="OLLAMA_HOST",
        api_key_env="",
        supports_reasoning=True,
        thinking_mode="provider_default",
    ),
    BuiltinModel(
        provider="fake",
        model_name="fake-model",
        context_length=8_192,
        display_name="Fake Model",
        api_key_env="",
        supports_reasoning=False,
        max_output_tokens=4096,
        reserved_output_tokens=4096,
    ),
)


DEFAULT_CONTEXT_LIMIT = 200_000
_DEFAULT_CONTEXT_LIMIT = DEFAULT_CONTEXT_LIMIT

# Model name -> context window in tokens. These catalogue aliases are matched
# first by exact name, then by prefix for dated/versioned provider model ids.
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
}


def get_model_context_limit(model_name: str) -> int:
    """Return the known context window size for *model_name*."""
    builtin_limits = {model.model_name: model.context_length for model in BUILTIN_MODELS}
    if model_name in builtin_limits:
        return builtin_limits[model_name]
    if model_name in MODEL_CONTEXT_LIMITS:
        return MODEL_CONTEXT_LIMITS[model_name]

    best_len = 0
    best_limit = DEFAULT_CONTEXT_LIMIT
    for key, limit in {**MODEL_CONTEXT_LIMITS, **builtin_limits}.items():
        if model_name.startswith(key) and len(key) > best_len:
            best_len = len(key)
            best_limit = limit
    return best_limit


def builtin_models_for_provider(provider: str) -> tuple[BuiltinModel, ...]:
    return tuple(model for model in BUILTIN_MODELS if model.provider == provider)


def builtin_model(provider: str, model_name: str) -> BuiltinModel | None:
    for model in BUILTIN_MODELS:
        if model.provider == provider and model.model_name == model_name:
            return model
    return None
