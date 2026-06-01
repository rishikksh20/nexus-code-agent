from __future__ import annotations

import os
from copy import deepcopy
from typing import Any, Callable

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static, TabbedContent, TabPane

from nexus.config.editor import delete_model_profile, save_model_profile, save_provider, set_active_model_profile
from nexus.config.loader import validate_profile_catalog
from nexus.config.provider_profiles import ModelProfile, ProviderConfig, ThinkingConfig
from nexus.integrations.registry import PROVIDER_DEFINITIONS, probe_model_profile


class ProviderSettingsScreen(Screen):
    """Configure fixed provider cards and reusable model profiles."""

    CSS = """
    ProviderSettingsScreen { padding: 1 2; }
    ProviderSettingsScreen VerticalScroll { height: 1fr; }
    ProviderSettingsScreen Horizontal { height: auto; margin: 1 0; }
    ProviderSettingsScreen Input, ProviderSettingsScreen Select { width: 1fr; margin: 0 1 1 0; }
    ProviderSettingsScreen Checkbox { margin: 0 1 1 0; }
    ProviderSettingsScreen Button { margin-right: 1; }
    #provider-status, #profile-status { min-height: 2; color: $text-muted; }
    """

    def __init__(self, state: Any, *, on_reload: Callable[[], None]) -> None:
        super().__init__()
        self.state = state
        self.on_reload = on_reload
        self._probe_confirmation = ""

    def compose(self) -> ComposeResult:
        provider_options = [(definition.display_name, name) for name, definition in PROVIDER_DEFINITIONS.items()]
        profile_options = [(name, name) for name in self.state.config.models]
        yield Label("Provider Management Profiles")
        with TabbedContent():
            with TabPane("Providers"):
                with VerticalScroll():
                    yield Select(provider_options, value=self.state.config.provider, id="provider-name")
                    yield Checkbox("Enabled", id="provider-enabled")
                    yield Input(placeholder="Base URL", id="provider-base-url")
                    yield Input(placeholder="API key environment variable", id="provider-api-key-env")
                    yield Input(placeholder="Timeout seconds", id="provider-timeout")
                    yield Input(placeholder="Max retries", id="provider-retries")
                    yield Input(placeholder="Retry base delay seconds", id="provider-retry-delay")
                    yield Input(placeholder="Retry jitter seconds", id="provider-retry-jitter")
                    yield Static("", id="credential-status")
                    with Horizontal():
                        yield Button("Save", id="provider-save", variant="primary")
                        yield Button("Enable / Disable", id="provider-toggle")
                        yield Button("Test", id="provider-test")
                    yield Static("", id="provider-status")
            with TabPane("Model Profiles"):
                with VerticalScroll():
                    yield Select(profile_options or [("No profiles yet", "")], value=self.state.config.active_model_profile or Select.BLANK, id="profile-select")
                    yield Input(placeholder="Profile name", id="profile-name")
                    yield Select(provider_options, value=self.state.config.provider, id="profile-provider")
                    yield Input(placeholder="Raw model name", id="profile-model-name")
                    yield Input(placeholder="Context length", id="profile-context")
                    yield Input(placeholder="Max output tokens", id="profile-max-output")
                    yield Input(placeholder="Reserved output tokens", id="profile-reserved-output")
                    yield Input(placeholder="Temperature", id="profile-temperature")
                    yield Input(placeholder="Top P", id="profile-top-p")
                    yield Checkbox("Supports tools", id="profile-tools")
                    yield Checkbox("Supports streaming", id="profile-streaming")
                    yield Checkbox("Supports reasoning", id="profile-reasoning")
                    yield Checkbox("Thinking enabled", id="thinking-enabled")
                    yield Select(
                        [("Provider default", "provider_default"), ("Budget tokens", "budget_tokens"), ("Reasoning effort", "reasoning_effort")],
                        value="provider_default",
                        id="thinking-mode",
                    )
                    yield Input(placeholder="Thinking budget tokens", id="thinking-budget")
                    yield Select([("None", ""), ("Low", "low"), ("Medium", "medium"), ("High", "high")], value="", id="thinking-effort")
                    with Horizontal():
                        yield Button("New", id="profile-new")
                        yield Button("Clone", id="profile-clone")
                        yield Button("Save", id="profile-save", variant="primary")
                        yield Button("Delete", id="profile-delete", variant="error")
                        yield Button("Activate", id="profile-activate", variant="success")
                        yield Button("Test", id="profile-test")
                    yield Static("", id="profile-status")
        yield Button("Close", id="close")

    def on_mount(self) -> None:
        self._load_provider(self.state.config.provider)
        active = str(self.state.config.active_model_profile or "")
        if active in self.state.config.models:
            self._load_profile(active)

    def on_select_changed(self, event: Select.Changed) -> None:
        value = str(event.value or "")
        if event.select.id == "provider-name" and value:
            self._load_provider(value)
        elif event.select.id == "profile-select" and value:
            self._load_profile(value)
        elif event.select.id == "profile-provider" and value:
            self._refresh_thinking_controls(value)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        try:
            if button_id == "close":
                self.dismiss()
            elif button_id == "provider-save":
                self._save_provider()
            elif button_id == "provider-toggle":
                self._toggle_provider()
            elif button_id == "provider-test":
                await self._confirm_or_probe("provider")
            elif button_id == "profile-new":
                self._new_profile()
            elif button_id == "profile-clone":
                self._clone_profile()
            elif button_id == "profile-save":
                self._save_profile()
            elif button_id == "profile-delete":
                self._delete_profile()
            elif button_id == "profile-activate":
                self._activate_profile()
            elif button_id == "profile-test":
                await self._confirm_or_probe("profile")
        except Exception as exc:  # noqa: BLE001
            self._status("provider" if button_id.startswith("provider") else "profile", str(exc), error=True)

    def _load_provider(self, name: str) -> None:
        provider = ProviderConfig.from_dict(name, self.state.config.providers.get(name, {}))
        self.query_one("#provider-name", Select).value = name
        self.query_one("#provider-enabled", Checkbox).value = provider.enabled
        self._input("#provider-base-url", provider.base_url)
        self._input("#provider-api-key-env", provider.api_key_env)
        self._input("#provider-timeout", provider.timeout_seconds)
        self._input("#provider-retries", provider.max_retries)
        self._input("#provider-retry-delay", provider.retry_base_delay_seconds)
        self._input("#provider-retry-jitter", provider.retry_jitter_seconds)
        resolved = bool(provider.api_key_env and os.environ.get(provider.api_key_env))
        self.query_one("#credential-status", Static).update(
            f"Credential env: {provider.api_key_env or 'not required'} ({'set' if resolved else 'not set'})"
        )

    def _save_provider(self) -> None:
        provider = self._provider_from_form()
        providers = deepcopy(self.state.config.providers)
        providers[provider.name] = provider.to_dict()
        validate_profile_catalog(providers, self.state.config.models, active_model_profile=self.state.config.active_model_profile)
        save_provider(self.state.config.global_config_file, provider)
        self.on_reload()
        self._status("provider", f"Saved {provider.name}.")

    def _toggle_provider(self) -> None:
        enabled = self.query_one("#provider-enabled", Checkbox)
        enabled.value = not enabled.value
        self._save_provider()

    def _provider_from_form(self) -> ProviderConfig:
        return ProviderConfig(
            name=str(self.query_one("#provider-name", Select).value),
            enabled=self.query_one("#provider-enabled", Checkbox).value,
            base_url=self._value("#provider-base-url"),
            api_key_env=self._value("#provider-api-key-env"),
            timeout_seconds=float(self._value("#provider-timeout") or 120),
            max_retries=int(self._value("#provider-retries") or 0),
            retry_base_delay_seconds=float(self._value("#provider-retry-delay") or 0),
            retry_jitter_seconds=float(self._value("#provider-retry-jitter") or 0),
        )

    def _load_profile(self, name: str) -> None:
        profile = ModelProfile.from_dict(name, self.state.config.models[name])
        self.query_one("#profile-select", Select).value = name
        self._input("#profile-name", profile.name)
        self.query_one("#profile-provider", Select).value = profile.provider
        self._input("#profile-model-name", profile.model_name)
        self._input("#profile-context", profile.context_length)
        self._input("#profile-max-output", profile.max_output_tokens)
        self._input("#profile-reserved-output", profile.reserved_output_tokens)
        self._input("#profile-temperature", profile.temperature)
        self._input("#profile-top-p", profile.top_p)
        self.query_one("#profile-tools", Checkbox).value = profile.supports_tools
        self.query_one("#profile-streaming", Checkbox).value = profile.supports_streaming
        self.query_one("#profile-reasoning", Checkbox).value = profile.supports_reasoning
        self.query_one("#thinking-enabled", Checkbox).value = profile.thinking.enabled
        self.query_one("#thinking-mode", Select).value = profile.thinking.mode
        self._input("#thinking-budget", profile.thinking.budget_tokens or "")
        self.query_one("#thinking-effort", Select).value = profile.thinking.reasoning_effort
        self._refresh_thinking_controls(profile.provider)

    def _new_profile(self) -> None:
        self._input("#profile-name", "")
        self._input("#profile-model-name", "")
        self._input("#profile-context", 32768)
        self._input("#profile-max-output", 4096)
        self._input("#profile-reserved-output", 4096)
        self._input("#profile-temperature", 0.0)
        self._input("#profile-top-p", 1.0)
        self.query_one("#profile-tools", Checkbox).value = True
        self.query_one("#profile-streaming", Checkbox).value = True
        self.query_one("#profile-reasoning", Checkbox).value = False
        self.query_one("#thinking-enabled", Checkbox).value = False
        self._status("profile", "Enter a profile name and raw model name.")

    def _clone_profile(self) -> None:
        source = str(self.query_one("#profile-select", Select).value or "")
        if not source:
            raise ValueError("Select a model profile to clone.")
        self._load_profile(source)
        self._input("#profile-name", f"{source}-copy")
        self._status("profile", "Cloned form. Save to create the profile.")

    def _save_profile(self) -> None:
        profile = self._profile_from_form()
        models = deepcopy(self.state.config.models)
        models[profile.name] = profile.to_dict()
        validate_profile_catalog(self.state.config.providers, models, active_model_profile=self.state.config.active_model_profile)
        save_model_profile(self.state.config.global_config_file, profile)
        self.on_reload()
        self._status("profile", f"Saved {profile.name}.")

    def _delete_profile(self) -> None:
        name = self._value("#profile-name")
        if not name:
            raise ValueError("Select a model profile to delete.")
        if name == self.state.config.active_model_profile:
            raise ValueError("Cannot delete the active model profile.")
        delete_model_profile(self.state.config.global_config_file, name)
        self.on_reload()
        self._status("profile", f"Deleted {name}.")

    def _activate_profile(self) -> None:
        name = self._value("#profile-name")
        if name not in self.state.config.models:
            raise ValueError(f"Save model profile '{name}' before activating it.")
        validate_profile_catalog(self.state.config.providers, self.state.config.models, active_model_profile=name)
        set_active_model_profile(self.state.config.local_config_file, name)
        self.on_reload()
        self._status("profile", f"Activated {name}.")

    def _profile_from_form(self) -> ModelProfile:
        budget = self._value("#thinking-budget")
        return ModelProfile(
            name=self._value("#profile-name"),
            provider=str(self.query_one("#profile-provider", Select).value),
            model_name=self._value("#profile-model-name"),
            context_length=int(self._value("#profile-context")),
            max_output_tokens=int(self._value("#profile-max-output")),
            reserved_output_tokens=int(self._value("#profile-reserved-output")),
            temperature=float(self._value("#profile-temperature")),
            top_p=float(self._value("#profile-top-p")),
            supports_tools=self.query_one("#profile-tools", Checkbox).value,
            supports_streaming=self.query_one("#profile-streaming", Checkbox).value,
            supports_reasoning=self.query_one("#profile-reasoning", Checkbox).value,
            thinking=ThinkingConfig(
                enabled=self.query_one("#thinking-enabled", Checkbox).value,
                mode=str(self.query_one("#thinking-mode", Select).value),
                budget_tokens=int(budget) if budget else None,
                reasoning_effort=str(self.query_one("#thinking-effort", Select).value or ""),
            ),
        )

    def _refresh_thinking_controls(self, provider_name: str) -> None:
        supported = PROVIDER_DEFINITIONS[provider_name].supported_thinking_modes
        disabled = not supported
        for selector in ("#thinking-enabled", "#thinking-mode", "#thinking-budget", "#thinking-effort"):
            self.query_one(selector).disabled = disabled

    async def _confirm_or_probe(self, kind: str) -> None:
        name = (
            self._value("#profile-name")
            if kind == "profile"
            else str(self.query_one("#provider-name", Select).value)
        )
        confirmation = f"{kind}:{name}"
        if self._probe_confirmation != confirmation:
            self._probe_confirmation = confirmation
            self._status(kind, "Live probe may incur a small charge. Click Test again to confirm.")
            return
        self._probe_confirmation = ""
        if kind == "provider":
            attached = next(
                (profile_name for profile_name, payload in self.state.config.models.items() if payload.get("provider") == name),
                "",
            )
            if not attached:
                raise ValueError("Save a model profile for this provider before running a live probe.")
            name = attached
        latency = await probe_model_profile(self.state.config, profile_name=name)
        self._status(kind, f"Probe succeeded in {latency * 1000:.0f} ms.")

    def _input(self, selector: str, value: object) -> None:
        self.query_one(selector, Input).value = str(value)

    def _value(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _status(self, kind: str, message: str, *, error: bool = False) -> None:
        self.query_one(f"#{kind}-status", Static).update(("Error: " if error else "") + message)
