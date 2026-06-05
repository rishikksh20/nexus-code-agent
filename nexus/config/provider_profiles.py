from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


PROVIDER_NAMES: tuple[str, ...] = (
    "anthropic",
    "cohere",
    "fake",
    "gemini",
    "mistral",
    "openai",
    "openai-compatible",
    "ollama",
)


@dataclass(slots=True)
class ProviderConfig:
    name: str
    enabled: bool = True
    base_url: str = ""
    api_key_env: str = ""
    timeout_seconds: float = 120.0
    max_retries: int = 3
    retry_base_delay_seconds: float = 0.5
    retry_jitter_seconds: float = 0.2

    @classmethod
    def from_dict(cls, name: str, payload: dict[str, Any] | None = None) -> "ProviderConfig":
        data = dict(payload or {})
        return cls(
            name=name,
            enabled=bool(data.get("enabled", True)),
            base_url=str(data.get("base_url", "") or ""),
            api_key_env=str(data.get("api_key_env", "") or ""),
            timeout_seconds=float(data.get("timeout_seconds", 120.0)),
            max_retries=int(data.get("max_retries", 3)),
            retry_base_delay_seconds=float(data.get("retry_base_delay_seconds", 0.5)),
            retry_jitter_seconds=float(data.get("retry_jitter_seconds", 0.2)),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("name", None)
        return payload


@dataclass(slots=True)
class ThinkingConfig:
    enabled: bool = False
    mode: str = "provider_default"
    budget_tokens: int | None = None
    reasoning_effort: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None = None) -> "ThinkingConfig":
        data = dict(payload or {})
        budget = data.get("budget_tokens")
        return cls(
            enabled=bool(data.get("enabled", False)),
            mode=str(data.get("mode", "provider_default") or "provider_default"),
            budget_tokens=int(budget) if budget is not None else None,
            reasoning_effort=str(data.get("reasoning_effort", "") or ""),
        )


@dataclass(slots=True)
class ModelProfile:
    name: str
    provider: str
    model_name: str
    context_length: int
    max_output_tokens: int
    reserved_output_tokens: int
    temperature: float = 0.0
    top_p: float = 1.0
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_reasoning: bool = False
    thinking: ThinkingConfig = field(default_factory=ThinkingConfig)

    @classmethod
    def from_dict(cls, name: str, payload: dict[str, Any]) -> "ModelProfile":
        max_output_tokens = int(payload.get("max_output_tokens", 4096))
        return cls(
            name=name,
            provider=str(payload.get("provider", "") or ""),
            model_name=str(payload.get("model_name", "") or ""),
            context_length=int(payload.get("context_length", 200_000)),
            max_output_tokens=max_output_tokens,
            reserved_output_tokens=int(payload.get("reserved_output_tokens", max_output_tokens)),
            temperature=float(payload.get("temperature", 0.0)),
            top_p=float(payload.get("top_p", 1.0)),
            supports_tools=bool(payload.get("supports_tools", True)),
            supports_streaming=bool(payload.get("supports_streaming", True)),
            supports_reasoning=bool(payload.get("supports_reasoning", False)),
            thinking=ThinkingConfig.from_dict(
                payload.get("thinking") if isinstance(payload.get("thinking"), dict) else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_name": self.model_name,
            "context_length": self.context_length,
            "max_output_tokens": self.max_output_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
            "supports_reasoning": self.supports_reasoning,
            "thinking": asdict(self.thinking),
        }


def deep_merge_named_tables(*catalogs: Any) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for catalog in catalogs:
        if not isinstance(catalog, dict):
            continue
        for name, raw in catalog.items():
            if not isinstance(raw, dict):
                continue
            current = dict(merged.get(str(name), {}))
            current = _deep_merge_dict(current, raw)
            merged[str(name)] = current
    return merged


def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def usable_prompt_budget(config: Any) -> int:
    profile = active_model_profile(config)
    return max(1, profile.context_length - profile.reserved_output_tokens)


def active_model_profile(config: Any) -> ModelProfile:
    name = str(getattr(config, "active_model_profile", "") or "").strip()
    models = getattr(config, "models", {})
    if name and isinstance(models, dict) and isinstance(models.get(name), dict):
        return ModelProfile.from_dict(name, models[name])
    return legacy_model_profile(config)


def active_provider_config(config: Any) -> ProviderConfig:
    profile = active_model_profile(config)
    providers = getattr(config, "providers", {})
    raw = providers.get(profile.provider, {}) if isinstance(providers, dict) else {}
    provider = ProviderConfig.from_dict(profile.provider, raw if isinstance(raw, dict) else {})
    effective_base_url = str(getattr(config, "api_base_url", "") or "")
    if effective_base_url:
        provider.base_url = effective_base_url
    return provider


def legacy_model_profile(config: Any, *, name: str = "legacy-current") -> ModelProfile:
    context_length = int(getattr(config, "context_length", 0) or 200_000)
    max_output_tokens = int(getattr(config, "max_output_tokens", 4096) or 4096)
    return ModelProfile(
        name=name,
        provider=str(getattr(config, "provider", "openai-compatible") or "openai-compatible"),
        model_name=str(getattr(config, "model_name", "") or ""),
        context_length=context_length,
        max_output_tokens=max_output_tokens,
        reserved_output_tokens=int(getattr(config, "reserved_output_tokens", 0) or max_output_tokens),
        temperature=float(getattr(config, "temperature", 0.0) or 0.0),
        top_p=float(getattr(config, "top_p", 1.0) or 1.0),
        supports_tools=bool(getattr(config, "supports_tools", True)),
        supports_streaming=bool(getattr(config, "supports_streaming", True)),
        supports_reasoning=bool(getattr(config, "supports_reasoning", False)),
        thinking=ThinkingConfig(
            enabled=str(getattr(config, "llm_thinking_mode", "auto")) == "enabled",
            mode=(
                "reasoning_effort"
                if str(getattr(config, "llm_reasoning_effort", "") or "")
                else "provider_default"
            ),
            reasoning_effort=str(getattr(config, "llm_reasoning_effort", "") or ""),
        ),
    )
