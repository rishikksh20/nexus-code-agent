from __future__ import annotations

import os
import time
from copy import copy
from dataclasses import dataclass
from typing import Any

from nexus.config.provider_profiles import (
    PROVIDER_NAMES,
    ModelProfile,
    ProviderConfig,
    active_model_profile,
    active_provider_config,
)
from nexus.integrations.anthropic import AnthropicModelClient, resolve_anthropic_api_key
from nexus.integrations.cohere import CohereModelClient, resolve_cohere_api_key
from nexus.integrations.fake_model import FakeModelClient
from nexus.integrations.gemini import GeminiModelClient, resolve_gemini_api_key
from nexus.integrations.ollama import OllamaModelClient, resolve_ollama_base_url
from nexus.integrations.openai_compatible import OpenAICompatibleModelClient, resolve_provider_api_key
from nexus.models import Message, RuntimeRequest, StreamEventType


@dataclass(slots=True, frozen=True)
class ProviderDefinition:
    name: str
    display_name: str
    description: str
    default_base_url: str = ""
    default_api_key_env: str = ""
    requires_api_key: bool = True
    supported_thinking_modes: tuple[str, ...] = ()


PROVIDER_DEFINITIONS: dict[str, ProviderDefinition] = {
    "anthropic": ProviderDefinition(
        "anthropic", "Anthropic", "Anthropic Messages API.",
        default_api_key_env="ANTHROPIC_API_KEY", supported_thinking_modes=("budget_tokens",),
    ),
    "cohere": ProviderDefinition(
        "cohere", "Cohere", "Cohere Chat API v2.", "https://api.cohere.com", "COHERE_API_KEY",
    ),
    "fake": ProviderDefinition(
        "fake", "Fake", "Deterministic offline development client.", requires_api_key=False,
    ),
    "gemini": ProviderDefinition(
        "gemini", "Gemini", "Google Gemini API.", default_api_key_env="GEMINI_API_KEY",
        supported_thinking_modes=("budget_tokens",),
    ),
    "mistral": ProviderDefinition(
        "mistral", "Mistral", "Mistral OpenAI-compatible API.", "https://api.mistral.ai/v1", "MISTRAL_API_KEY",
        supported_thinking_modes=("reasoning_effort",),
    ),
    "openai": ProviderDefinition(
        "openai", "OpenAI", "OpenAI Chat Completions API.", "https://api.openai.com/v1", "OPENAI_API_KEY",
        supported_thinking_modes=("reasoning_effort",),
    ),
    "openai-compatible": ProviderDefinition(
        "openai-compatible", "OpenAI Compatible", "Custom OpenAI-compatible Chat Completions API.",
        default_api_key_env="API_KEY", supported_thinking_modes=("provider_default", "budget_tokens", "reasoning_effort"),
    ),
    "ollama": ProviderDefinition(
        "ollama", "Ollama", "Local Ollama native chat API.", "http://localhost:11434", "",
        requires_api_key=False, supported_thinking_modes=("provider_default", "reasoning_effort"),
    ),
}


def provider_defaults() -> dict[str, dict[str, Any]]:
    return {
        name: ProviderConfig(
            name=name,
            enabled=True,
            base_url=definition.default_base_url,
            api_key_env=definition.default_api_key_env,
            timeout_seconds=300.0 if name == "ollama" else 120.0,
            max_retries=0 if name == "ollama" else 3,
        ).to_dict()
        for name, definition in PROVIDER_DEFINITIONS.items()
    }


def provider_definition(name: str) -> ProviderDefinition:
    try:
        return PROVIDER_DEFINITIONS[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown provider '{name}'. Available providers: {', '.join(PROVIDER_NAMES)}."
        ) from exc


def resolve_configured_api_key(config: Any, provider: ProviderConfig) -> str | None:
    explicit = str(getattr(config, "api_key", "") or "") or None
    if provider.api_key_env:
        configured = os.environ.get(provider.api_key_env)
        if configured:
            return configured
    if provider.name == "anthropic":
        return resolve_anthropic_api_key(explicit)
    if provider.name == "cohere":
        return resolve_cohere_api_key(explicit)
    if provider.name == "gemini":
        return resolve_gemini_api_key(explicit)
    if provider.name in {"mistral", "openai", "openai-compatible"}:
        return resolve_provider_api_key(provider.name, explicit)
    return explicit


def provider_has_api_key(config: Any) -> bool:
    provider = active_provider_config(config)
    definition = provider_definition(provider.name)
    return not definition.requires_api_key or bool(resolve_configured_api_key(config, provider))


def build_model_client(config: Any):
    profile = active_model_profile(config)
    provider = active_provider_config(config)
    if not provider.enabled:
        raise ValueError(f"Provider '{provider.name}' is disabled.")
    thinking = profile.thinking
    explicit_key = resolve_configured_api_key(config, provider)
    common = {
        "timeout_seconds": provider.timeout_seconds,
    }
    retry = {
        "retries": provider.max_retries,
        "base_delay": provider.retry_base_delay_seconds,
        "jitter": provider.retry_jitter_seconds,
    }
    if provider.name == "fake":
        return FakeModelClient()
    if provider.name == "ollama":
        return OllamaModelClient(
            base_url=resolve_ollama_base_url(provider.base_url or None),
            model_name=profile.model_name,
            thinking=thinking,
            **common,
            **retry,
        )
    if provider.name == "anthropic":
        return AnthropicModelClient(api_key=explicit_key, thinking=thinking, **common)
    if provider.name == "cohere":
        if thinking.enabled:
            raise ValueError("Provider 'cohere' does not support thinking-enabled model profiles.")
        return CohereModelClient(api_base_url=provider.base_url, api_key=explicit_key, **common, **retry)
    if provider.name == "gemini":
        return GeminiModelClient(api_key=explicit_key, thinking=thinking)
    if provider.name in {"mistral", "openai-compatible", "openai"}:
        thinking_mode = (
            str(getattr(config, "llm_thinking_mode", "auto") or "auto")
            if bool(getattr(config, "active_model_profile_legacy", True))
            else ("enabled" if thinking.enabled else "auto")
        )
        return OpenAICompatibleModelClient(
            api_base_url=provider.base_url,
            api_key=explicit_key,
            provider_name=provider.name,
            thinking_mode=thinking_mode,
            reasoning_effort=thinking.reasoning_effort,
            thinking=thinking,
            **common,
            **retry,
        )
    raise ValueError(f"Unsupported provider: {provider.name}")


async def probe_model_profile(
    config: Any,
    *,
    profile_name: str = "",
    max_output_tokens: int = 8,
) -> float:
    effective = copy(config)
    if profile_name:
        if profile_name not in effective.models:
            raise ValueError(f"Unknown model profile '{profile_name}'.")
        effective.active_model_profile = profile_name
        effective.active_model_profile_legacy = False
        profile = active_model_profile(effective)
        provider = ProviderConfig.from_dict(profile.provider, effective.providers.get(profile.provider, {}))
        effective.provider = profile.provider
        effective.model_name = profile.model_name
        effective.api_base_url = provider.base_url
    profile: ModelProfile = active_model_profile(effective)
    client = build_model_client(effective)
    started_at = time.perf_counter()
    request = RuntimeRequest(
        model_name=profile.model_name,
        system_prompt="Reply with OK.",
        messages=(Message(role="user", content="OK"),),
        max_output_tokens=max_output_tokens,
        temperature=0.0,
        top_p=profile.top_p,
        thinking=profile.thinking,
    )
    async for event in client.chat_completion(request, stream=False):
        if event.type == StreamEventType.ERROR:
            raise RuntimeError(event.error or "Provider probe failed.")
    return time.perf_counter() - started_at
