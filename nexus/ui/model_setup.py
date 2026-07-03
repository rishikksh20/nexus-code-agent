from __future__ import annotations

import os
import re
from copy import deepcopy
from typing import Any, Callable

from textual import events
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from nexus.config.editor import (
    set_active_model_profile,
    update_dotenv_value,
    update_model_profile_fields,
    update_provider_fields,
)
from nexus.config.loader import validate_profile_catalog
from nexus.config.model_catalog import (
    BuiltinModel,
    builtin_model,
    builtin_models_for_provider,
    get_model_context_limit,
)
from nexus.config.provider_profiles import ModelProfile, ProviderConfig, ThinkingConfig
from nexus.integrations.registry import PROVIDER_DEFINITIONS


_CUSTOM_MODEL = "custom:"
_PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
_MODE_LABELS = {
    "provider_default": "Provider default",
    "budget_tokens": "Budget tokens",
    "reasoning_effort": "Reasoning effort",
}
_REASONING_EFFORTS = (
    ("None", ""),
    ("Low", "low"),
    ("Medium", "medium"),
    ("High", "high"),
    ("Extra high", "xhigh"),
    ("Max", "max"),
)


class ModelSetupScreen(Screen):
    """Workspace-first setup wizard for provider, model, thinking, and API key."""

    BINDINGS = [
        ("ctrl+v", "paste_focused_input", "Paste field"),
        ("ctrl+shift+v", "paste_focused_input", "Paste field"),
        ("ctrl+c", "copy_focused_input", "Copy field"),
        ("ctrl+x", "cut_focused_input", "Cut field"),
        ("ctrl+shift+a", "select_focused_input", "Select field"),
    ]

    CSS = """
    ModelSetupScreen { padding: 0; }
    #setup-shell { width: 100%; height: 100%; padding: 1 2; }
    #setup-scroll { height: 1fr; }
    .setup-row { height: auto; margin: 0 0 1 0; }
    #setup-title { text-style: bold; color: #ff79c6; margin-bottom: 1; }
    .setup-key { width: 24; content-align: left middle; text-style: bold; }
    .setup-key-blue { color: #6ea8ff; }
    .setup-key-green { color: #4ade80; }
    .setup-key-pink { color: #ff79c6; }
    .setup-value { width: 1fr; }
    #setup-extra-pair { width: 1fr; height: auto; }
    #setup-extra-key { width: 1fr; margin-right: 1; }
    #setup-extra-value { width: 2fr; }
    ModelSetupScreen Input, ModelSetupScreen Select { width: 1fr; }
    ModelSetupScreen Checkbox { margin: 0; }
    ModelSetupScreen Button { margin-right: 1; }
    #setup-status, #setup-credential-status { min-height: 2; color: $text-muted; }
    """

    def __init__(self, state: Any, *, on_reload: Callable[[], None]) -> None:
        super().__init__()
        self.state = state
        self.on_reload = on_reload

    def compose(self) -> ComposeResult:
        provider_options = [(definition.display_name, name) for name, definition in PROVIDER_DEFINITIONS.items()]
        provider_name = str(self.state.config.provider or "openai-compatible")
        with Container(id="setup-shell"):
            yield Label("Model Provider Setup", id="setup-title")
            with VerticalScroll(id="setup-scroll"):
                with Horizontal(classes="setup-row"):
                    yield self._key("Provider", "blue")
                    yield Select(provider_options, value=provider_name, id="setup-provider", classes="setup-value")
                with Horizontal(classes="setup-row"):
                    yield self._key("Model", "green")
                    yield Select(self._model_options(provider_name), id="setup-model-choice", classes="setup-value")
                with Horizontal(id="setup-profile-row", classes="setup-row"):
                    yield self._key("Profile", "pink")
                    yield Input(placeholder="profile-name", id="setup-profile-name", classes="setup-value")
                with Horizontal(classes="setup-row"):
                    yield self._key("Model name", "blue")
                    yield Input(placeholder="model-name", id="setup-model-name", classes="setup-value")
                with Horizontal(id="setup-base-url-row", classes="setup-row"):
                    yield self._key("Base URL", "green")
                    yield Input(placeholder="https://...", id="setup-base-url", classes="setup-value")
                with Horizontal(id="setup-base-url-env-row", classes="setup-row"):
                    yield self._key("Base URL env", "pink")
                    yield Input(placeholder="BASE_URL", id="setup-base-url-env", classes="setup-value")
                with Horizontal(classes="setup-row"):
                    yield self._key("Context tokens", "blue")
                    yield Input(placeholder="200000", id="setup-context", classes="setup-value")
                with Horizontal(classes="setup-row"):
                    yield self._key("Max output", "green")
                    yield Input(placeholder="4096", id="setup-max-output", classes="setup-value")
                with Horizontal(classes="setup-row"):
                    yield self._key("Reserved output", "pink")
                    yield Input(placeholder="4096", id="setup-reserved-output", classes="setup-value")
                with Horizontal(classes="setup-row"):
                    yield self._key("Temperature", "blue")
                    yield Input(placeholder="0.0", id="setup-temperature", classes="setup-value")
                with Horizontal(classes="setup-row"):
                    yield self._key("Top P", "green")
                    yield Input(placeholder="1.0", id="setup-top-p", classes="setup-value")
                with Horizontal(id="setup-capability-row", classes="setup-row"):
                    yield self._key("Capabilities", "pink")
                    with Horizontal(classes="setup-value"):
                        yield Checkbox("Tools", id="setup-tools")
                        yield Checkbox("Streaming", id="setup-streaming")
                        yield Checkbox("Reasoning", id="setup-reasoning")
                with Horizontal(id="setup-thinking-enabled-row", classes="setup-row"):
                    yield self._key("Thinking", "blue")
                    yield Checkbox("Enabled", id="setup-thinking-enabled", classes="setup-value")
                with Horizontal(id="setup-thinking-mode-row", classes="setup-row"):
                    yield self._key("Thinking mode", "green")
                    yield Select([("Provider default", "provider_default")], value="provider_default", id="setup-thinking-mode", classes="setup-value")
                with Horizontal(id="setup-thinking-budget-row", classes="setup-row"):
                    yield self._key("Thinking budget", "pink")
                    yield Input(placeholder="1024", id="setup-thinking-budget", classes="setup-value")
                with Horizontal(id="setup-thinking-effort-row", classes="setup-row"):
                    yield self._key("Reasoning effort", "blue")
                    yield Select(_REASONING_EFFORTS, value="", id="setup-thinking-effort", classes="setup-value")
                with Horizontal(id="setup-api-key-env-row", classes="setup-row"):
                    yield self._key("API key env", "green")
                    yield Input(placeholder="API_KEY", id="setup-api-key-env", classes="setup-value")
                with Horizontal(id="setup-api-key-row", classes="setup-row"):
                    yield self._key("API key", "pink")
                    yield Input(placeholder="secret value", password=True, id="setup-api-key", classes="setup-value")
                with Horizontal(id="setup-extra-row", classes="setup-row"):
                    yield self._key("Extra .env", "blue")
                    with Horizontal(id="setup-extra-pair", classes="setup-value"):
                        yield Input(placeholder="KEY", id="setup-extra-key")
                        yield Input(placeholder="value", id="setup-extra-value")
                with Horizontal(classes="setup-row"):
                    yield Static("", classes="setup-key")
                    yield Button("Add .env Field", id="setup-add-field")
                with Horizontal(classes="setup-row"):
                    yield Static("", classes="setup-key")
                    yield Static("", id="setup-credential-status", classes="setup-value")
                with Horizontal(classes="setup-row"):
                    yield Static("", classes="setup-key")
                    yield Button("Save and Activate", id="setup-save", variant="primary")
                    yield Button("Close", id="setup-close")
                with Horizontal(classes="setup-row"):
                    yield Static("", classes="setup-key")
                    yield Static("", id="setup-status", classes="setup-value")

    def on_mount(self) -> None:
        self.query_one("#setup-profile-row").display = False
        self.query_one("#setup-capability-row").display = False
        self.query_one("#setup-extra-row").display = False
        provider_name = str(self.state.config.provider or "openai-compatible")
        self._load_provider(provider_name)
        active_profile = str(self.state.config.active_model_profile or "")
        if active_profile in self.state.config.models:
            self._load_model_choice(f"profile:{active_profile}")
            self.query_one("#setup-model-choice", Select).value = f"profile:{active_profile}"
        else:
            self._load_model_choice(_CUSTOM_MODEL)

    def on_select_changed(self, event: Select.Changed) -> None:
        value = str(event.value or "")
        if event.select.id == "setup-provider" and value:
            self._load_provider(value)
            model_select = self.query_one("#setup-model-choice", Select)
            options = self._model_options(value)
            model_select.set_options(options)
            next_value = options[0][1] if options else _CUSTOM_MODEL
            model_select.value = next_value
            self._load_model_choice(str(next_value))
        elif event.select.id == "setup-model-choice" and value:
            self._load_model_choice(value)
        elif event.select.id == "setup-thinking-mode":
            self._refresh_thinking_controls()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "setup-thinking-enabled":
            if event.value:
                self.query_one("#setup-reasoning", Checkbox).value = True
            self._refresh_thinking_controls()

    def on_paste(self, event: events.Paste) -> None:
        focused = self._focused_setup_input()
        if focused is None or not str(getattr(event, "text", "") or ""):
            return
        self._paste_text_into_input(focused, str(event.text))
        event.stop()

    def action_paste_focused_input(self) -> None:
        focused = self._focused_setup_input()
        if focused is None:
            return
        clipboard = str(getattr(self.app, "clipboard", "") or "")
        if clipboard:
            self._paste_text_into_input(focused, clipboard)

    def action_copy_focused_input(self) -> None:
        focused = self._focused_setup_input()
        if focused is None:
            return
        selected_text = str(getattr(focused, "selected_text", "") or "")
        if selected_text:
            self._copy_text(selected_text)

    def action_cut_focused_input(self) -> None:
        focused = self._focused_setup_input()
        if focused is None:
            return
        selected_text = str(getattr(focused, "selected_text", "") or "")
        if not selected_text:
            return
        self._copy_text(selected_text)
        focused.replace("", *focused.selection)

    def action_select_focused_input(self) -> None:
        focused = self._focused_setup_input()
        if focused is not None:
            focused.select_all()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        try:
            if button_id == "setup-close":
                self.dismiss()
            elif button_id == "setup-add-field":
                self.query_one("#setup-extra-row").display = True
                self.query_one("#setup-extra-key", Input).focus()
            elif button_id == "setup-save":
                self._save_setup()
        except Exception as exc:  # noqa: BLE001
            self._status(str(exc), error=True)

    def _load_provider(self, name: str) -> None:
        provider = ProviderConfig.from_dict(name, self.state.config.providers.get(name, {}))
        definition = PROVIDER_DEFINITIONS[name]
        self.query_one("#setup-provider", Select).value = name
        self._input("#setup-base-url", provider.base_url or definition.default_base_url)
        self._input("#setup-base-url-env", _default_base_url_env(name))
        self._input("#setup-api-key-env", provider.api_key_env or definition.default_api_key_env)
        api_key_input = self.query_one("#setup-api-key", Input)
        api_key_input.disabled = not definition.requires_api_key
        if not definition.requires_api_key:
            api_key_input.value = ""
        self._refresh_credential_status()
        self._set_thinking_mode_options(name)
        self._refresh_visible_fields()

    def _load_model_choice(self, choice: str) -> None:
        if choice.startswith("profile:"):
            name = choice.removeprefix("profile:")
            if name in self.state.config.models:
                self._load_profile(name)
            return
        provider_name = str(self.query_one("#setup-provider", Select).value)
        if choice.startswith("builtin:"):
            model_name = choice.removeprefix("builtin:")
            self._load_builtin_model(provider_name, model_name)
            return
        self._new_custom_model(provider_name)

    def _load_profile(self, name: str) -> None:
        profile = ModelProfile.from_dict(name, self.state.config.models[name])
        self.query_one("#setup-provider", Select).value = profile.provider
        self._load_provider(profile.provider)
        entry = builtin_model(profile.provider, profile.model_name)
        self._input("#setup-profile-name", profile.name)
        self._input("#setup-model-name", profile.model_name)
        if entry is not None:
            self._input("#setup-base-url", entry.base_url or PROVIDER_DEFINITIONS[entry.provider].default_base_url)
            self._input("#setup-base-url-env", entry.base_url_env or _default_base_url_env(entry.provider))
            self._input("#setup-api-key-env", entry.api_key_env or PROVIDER_DEFINITIONS[entry.provider].default_api_key_env)
        self._input("#setup-context", profile.context_length)
        self._input("#setup-max-output", profile.max_output_tokens)
        self._input("#setup-reserved-output", profile.reserved_output_tokens)
        self._input("#setup-temperature", profile.temperature)
        self._input("#setup-top-p", profile.top_p)
        self.query_one("#setup-tools", Checkbox).value = profile.supports_tools
        self.query_one("#setup-streaming", Checkbox).value = profile.supports_streaming
        self.query_one("#setup-reasoning", Checkbox).value = profile.supports_reasoning
        self.query_one("#setup-thinking-enabled", Checkbox).value = profile.thinking.enabled
        self._set_thinking_mode_options(profile.provider, selected=profile.thinking.mode)
        self._input("#setup-thinking-budget", profile.thinking.budget_tokens or "")
        self.query_one("#setup-thinking-effort", Select).value = profile.thinking.reasoning_effort
        self._refresh_thinking_controls()
        self._refresh_visible_fields()

    def _load_builtin_model(self, provider_name: str, model_name: str) -> None:
        entry = builtin_model(provider_name, model_name)
        if entry is None:
            entry = BuiltinModel(
                provider=provider_name,
                model_name=model_name,
                context_length=get_model_context_limit(model_name),
                supports_reasoning=bool(PROVIDER_DEFINITIONS[provider_name].supported_thinking_modes),
            )
        self._input("#setup-profile-name", _profile_name_for(entry.provider, entry.model_name))
        self._input("#setup-model-name", entry.model_name)
        self._input("#setup-base-url", entry.base_url or PROVIDER_DEFINITIONS[entry.provider].default_base_url)
        self._input("#setup-base-url-env", entry.base_url_env or _default_base_url_env(entry.provider))
        self._input("#setup-api-key-env", entry.api_key_env or PROVIDER_DEFINITIONS[entry.provider].default_api_key_env)
        self._input("#setup-context", entry.context_length)
        self._input("#setup-max-output", min(entry.max_output_tokens, max(1, entry.context_length - 1)))
        self._input("#setup-reserved-output", min(entry.reserved_output_tokens, max(1, entry.context_length - 1)))
        self._input("#setup-temperature", entry.temperature)
        self._input("#setup-top-p", entry.top_p)
        self.query_one("#setup-tools", Checkbox).value = entry.supports_tools
        self.query_one("#setup-streaming", Checkbox).value = entry.supports_streaming
        self.query_one("#setup-reasoning", Checkbox).value = entry.supports_reasoning
        self.query_one("#setup-thinking-enabled", Checkbox).value = False
        self._set_thinking_mode_options(entry.provider, selected=entry.thinking_mode)
        self._input("#setup-thinking-budget", entry.thinking_budget_tokens or "")
        self.query_one("#setup-thinking-effort", Select).value = entry.reasoning_effort
        self._refresh_thinking_controls()
        self._refresh_visible_fields()

    def _new_custom_model(self, provider_name: str) -> None:
        self._input("#setup-profile-name", "")
        self._input("#setup-model-name", "")
        self._input("#setup-base-url-env", _default_base_url_env(provider_name))
        self._input("#setup-context", 200000)
        self._input("#setup-max-output", 4096)
        self._input("#setup-reserved-output", 4096)
        self._input("#setup-temperature", 0.0)
        self._input("#setup-top-p", 1.0)
        self.query_one("#setup-tools", Checkbox).value = True
        self.query_one("#setup-streaming", Checkbox).value = True
        self.query_one("#setup-reasoning", Checkbox).value = bool(PROVIDER_DEFINITIONS[provider_name].supported_thinking_modes)
        self.query_one("#setup-thinking-enabled", Checkbox).value = False
        self._set_thinking_mode_options(provider_name)
        self._refresh_thinking_controls()
        self._refresh_visible_fields()

    def _save_setup(self) -> None:
        provider = self._provider_from_form()
        profile = self._profile_from_form(provider.name)
        providers = deepcopy(self.state.config.providers)
        providers[provider.name] = provider.to_dict()
        models = deepcopy(self.state.config.models)
        models[profile.name] = profile.to_dict()
        validate_profile_catalog(providers, models, active_model_profile=profile.name)
        update_provider_fields(self.state.config.local_config_file, provider.name, provider.to_dict())
        update_model_profile_fields(self.state.config.local_config_file, profile.name, profile.to_dict())
        set_active_model_profile(self.state.config.local_config_file, profile.name)
        base_url = self._value("#setup-base-url")
        base_url_env = self._value("#setup-base-url-env")
        if base_url and base_url_env:
            _validate_env_key(base_url_env, "Base URL env")
            update_dotenv_value(self.state.config.workspace_root / ".env", base_url_env, base_url)
            os.environ[base_url_env] = base_url
        api_key = self._value("#setup-api-key")
        if api_key and provider.api_key_env:
            update_dotenv_value(self.state.config.workspace_root / ".env", provider.api_key_env, api_key)
            os.environ[provider.api_key_env] = api_key
        extra_key = self._value("#setup-extra-key")
        extra_value = self._value("#setup-extra-value")
        if extra_key or extra_value:
            _validate_env_key(extra_key, "Extra .env key")
            update_dotenv_value(self.state.config.workspace_root / ".env", extra_key, extra_value)
            os.environ[extra_key] = extra_value
        self.on_reload()
        self._status(f"Saved and activated {profile.name}.")
        self._refresh_credential_status()

    def _provider_from_form(self) -> ProviderConfig:
        name = str(self.query_one("#setup-provider", Select).value)
        current = ProviderConfig.from_dict(name, self.state.config.providers.get(name, {}))
        api_key_env = self._value("#setup-api-key-env")
        if PROVIDER_DEFINITIONS[name].requires_api_key and not api_key_env:
            raise ValueError("API key environment variable is required for this provider.")
        if api_key_env:
            _validate_env_key(api_key_env, "API key env")
        return ProviderConfig(
            name=name,
            enabled=True,
            base_url=self._value("#setup-base-url"),
            api_key_env=api_key_env,
            timeout_seconds=current.timeout_seconds,
            max_retries=current.max_retries,
            retry_base_delay_seconds=current.retry_base_delay_seconds,
            retry_jitter_seconds=current.retry_jitter_seconds,
        )

    def _profile_from_form(self, provider_name: str) -> ModelProfile:
        model_name = self._value("#setup-model-name")
        if not model_name:
            raise ValueError("Model name is required.")
        profile_name = self._value("#setup-profile-name") or _profile_name_for(provider_name, model_name)
        if not _PROFILE_NAME_RE.fullmatch(profile_name):
            raise ValueError("Profile name must use letters, numbers, hyphens, or underscores.")
        context_length = _positive_int(self._value("#setup-context"), "Context length")
        max_output_tokens = _positive_int(self._value("#setup-max-output"), "Max output tokens")
        reserved_output_tokens = _positive_int(self._value("#setup-reserved-output"), "Reserved output tokens")
        if max_output_tokens > reserved_output_tokens:
            raise ValueError("Max output tokens must be less than or equal to reserved output tokens.")
        if reserved_output_tokens >= context_length:
            raise ValueError("Reserved output tokens must be less than context length.")
        thinking_enabled = self.query_one("#setup-thinking-enabled", Checkbox).value
        thinking_mode = str(self.query_one("#setup-thinking-mode", Select).value or "provider_default")
        budget = self._value("#setup-thinking-budget")
        effort = str(self.query_one("#setup-thinking-effort", Select).value or "")
        if thinking_enabled and thinking_mode == "budget_tokens" and not budget:
            budget = "1024"
        if thinking_enabled and thinking_mode == "reasoning_effort" and not effort:
            effort = "high"
        return ModelProfile(
            name=profile_name,
            provider=provider_name,
            model_name=model_name,
            context_length=context_length,
            max_output_tokens=max_output_tokens,
            reserved_output_tokens=reserved_output_tokens,
            temperature=float(self._value("#setup-temperature")),
            top_p=float(self._value("#setup-top-p")),
            supports_tools=self.query_one("#setup-tools", Checkbox).value,
            supports_streaming=self.query_one("#setup-streaming", Checkbox).value,
            supports_reasoning=self.query_one("#setup-reasoning", Checkbox).value or thinking_enabled,
            thinking=ThinkingConfig(
                enabled=thinking_enabled,
                mode=thinking_mode,
                budget_tokens=int(budget) if budget else None,
                reasoning_effort=effort,
            ),
        )

    def _set_thinking_mode_options(self, provider_name: str, *, selected: str = "") -> None:
        supported = PROVIDER_DEFINITIONS[provider_name].supported_thinking_modes
        modes = supported or ("provider_default",)
        select = self.query_one("#setup-thinking-mode", Select)
        select.set_options([(_MODE_LABELS.get(mode, mode), mode) for mode in modes])
        select.value = selected if selected in modes else modes[0]
        if not supported:
            self.query_one("#setup-thinking-enabled", Checkbox).value = False
        self._refresh_thinking_controls()

    def _refresh_thinking_controls(self) -> None:
        provider_name = str(self.query_one("#setup-provider", Select).value or "")
        supported = PROVIDER_DEFINITIONS[provider_name].supported_thinking_modes if provider_name else ()
        thinking_enabled = self.query_one("#setup-thinking-enabled", Checkbox).value
        mode = str(self.query_one("#setup-thinking-mode", Select).value or "provider_default")
        disabled = not supported
        self.query_one("#setup-thinking-enabled", Checkbox).disabled = disabled
        self.query_one("#setup-thinking-mode", Select).disabled = disabled or not thinking_enabled
        self.query_one("#setup-thinking-budget", Input).disabled = disabled or not thinking_enabled or mode != "budget_tokens"
        self.query_one("#setup-thinking-effort", Select).disabled = disabled or not thinking_enabled or mode != "reasoning_effort"
        self._set_row_visible("#setup-thinking-enabled-row", bool(supported))
        self._set_row_visible("#setup-thinking-mode-row", bool(supported) and thinking_enabled)
        self._set_row_visible("#setup-thinking-budget-row", bool(supported) and thinking_enabled and mode == "budget_tokens")
        self._set_row_visible("#setup-thinking-effort-row", bool(supported) and thinking_enabled and mode == "reasoning_effort")
        if thinking_enabled and mode == "budget_tokens" and not self._value("#setup-thinking-budget"):
            self._input("#setup-thinking-budget", 1024)
        if thinking_enabled and mode == "reasoning_effort" and not self.query_one("#setup-thinking-effort", Select).value:
            self.query_one("#setup-thinking-effort", Select).value = "high"

    def _model_options(self, provider_name: str) -> list[tuple[str, str]]:
        options: list[tuple[str, str]] = []
        for name, payload in sorted(self.state.config.models.items()):
            if payload.get("provider") == provider_name:
                model = str(payload.get("model_name", ""))
                options.append((f"Existing: {name} ({model})", f"profile:{name}"))
        for entry in builtin_models_for_provider(provider_name):
            label = entry.display_name or entry.model_name
            api = "OpenAI-compatible" if entry.openai_compatible else "native"
            thinking = "thinking" if entry.supports_reasoning else "standard"
            options.append((f"Builtin: {label} ({api}, {thinking})", f"builtin:{entry.model_name}"))
        options.append(("Add new model...", _CUSTOM_MODEL))
        return options

    def _refresh_credential_status(self) -> None:
        provider_name = str(self.query_one("#setup-provider", Select).value or "")
        if not provider_name:
            return
        definition = PROVIDER_DEFINITIONS[provider_name]
        env_name = self._value("#setup-api-key-env")
        if not definition.requires_api_key:
            text = "No API key required."
        else:
            text = f"Credential env: {env_name or 'unset'} ({'set' if env_name and os.environ.get(env_name) else 'not set'})"
        self.query_one("#setup-credential-status", Static).update(text)

    def _input(self, selector: str, value: object) -> None:
        self.query_one(selector, Input).value = str(value)

    def _value(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _status(self, message: str, *, error: bool = False) -> None:
        self.query_one("#setup-status", Static).update(("Error: " if error else "") + message)

    def _key(self, label: str, tone: str) -> Static:
        return Static(label, classes=f"setup-key setup-key-{tone}")

    def _refresh_visible_fields(self) -> None:
        provider_name = str(self.query_one("#setup-provider", Select).value or "")
        definition = PROVIDER_DEFINITIONS[provider_name]
        has_base_url = bool(self._value("#setup-base-url") or _default_base_url_env(provider_name))
        self._set_row_visible("#setup-base-url-row", has_base_url)
        self._set_row_visible("#setup-base-url-env-row", False)
        self._set_row_visible("#setup-api-key-env-row", False)
        self._set_row_visible("#setup-api-key-row", definition.requires_api_key)

    def _set_row_visible(self, selector: str, visible: bool) -> None:
        self.query_one(selector).display = visible

    def _focused_setup_input(self) -> Input | None:
        focused = self.app.focused or getattr(self, "focused", None)
        if isinstance(focused, Input) and str(focused.id or "").startswith("setup-"):
            return focused
        return None

    def _paste_text_into_input(self, input_widget: Input, text: str) -> None:
        line = text.splitlines()[0]
        selection = input_widget.selection
        if selection.is_empty:
            input_widget.insert_text_at_cursor(line)
        else:
            input_widget.replace(line, *selection)

    def _copy_text(self, text: str) -> None:
        copy_to_clipboard = getattr(self.app, "_copy_text_to_clipboard", None)
        if callable(copy_to_clipboard):
            copy_to_clipboard(text)
        else:
            self.app.copy_to_clipboard(text)


def _profile_name_for(provider_name: str, model_name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", f"{provider_name}-{model_name}").strip("-").lower()
    return slug or "model-profile"


def _default_base_url_env(provider_name: str) -> str:
    if provider_name == "mistral":
        return "MISTRAL_BASE_URL"
    if provider_name == "cohere":
        return "COHERE_BASE_URL"
    if provider_name == "ollama":
        return "OLLAMA_HOST"
    if provider_name in {"openai", "openai-compatible"}:
        return "BASE_URL"
    return ""


def _positive_int(raw_value: str, label: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if value <= 0:
        raise ValueError(f"{label} must be greater than 0.")
    return value


def _validate_env_key(value: str, label: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"{label} must be a valid environment variable name.")
