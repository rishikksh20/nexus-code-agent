# Mistral Setup For Nexus

This guide shows how to use Mistral as the LLM provider for this repo.

## Summary

Nexus now supports `mistral` as a provider. It uses the existing OpenAI-compatible client boundary, so Mistral works through the same normalized runtime request and response types already used for the other live providers.

Default base URL:

```text
https://api.mistral.ai/v1
```

Default behavior:

- provider name: `mistral`
- API key env var: `MISTRAL_API_KEY`
- base URL env var override: `MISTRAL_BASE_URL`
- generic base URL override: `AGENT_API_BASE_URL`

## Quick Start

Export your API key:

```bash
export MISTRAL_API_KEY="your-token"
```

Run Nexus with a Mistral model:

```bash
uv run nexus --provider mistral --model mistral-small-latest --prompt "summarize this repo"
```

## Config File Setup

You can set Mistral in `.nexus/config.toml` or `~/.nexus/config.toml`.

Example:

```toml
provider = "mistral"
model_name = "mistral-small-latest"
# Optional when using the official Mistral endpoint.
api_base_url = "https://api.mistral.ai/v1"
```

Notes:

- `api_base_url` is optional for `mistral` because Nexus defaults it to the official Mistral endpoint.
- If you set `api_base_url` in config, it takes precedence over the built-in default.
- If you export `AGENT_API_BASE_URL`, it overrides config just like other `AGENT_*` values.
- If `api_base_url` is still empty after config/env resolution, Nexus falls back to `MISTRAL_BASE_URL` and then the official default.

## Environment Variables

### Required

```bash
export MISTRAL_API_KEY="your-token"
```

### Optional

Override the Mistral base URL:

```bash
export MISTRAL_BASE_URL="https://api.mistral.ai/v1"
```

Or use the generic Nexus config env override:

```bash
export AGENT_API_BASE_URL="https://api.mistral.ai/v1"
```

## Example Local Config

```toml
provider = "mistral"
model_name = "mistral-small-latest"
default_mode = "default"
allowed_tools = ["get_time", "write_note"]
```

## Example Commands

Run a single prompt:

```bash
uv run nexus --provider mistral --model mistral-small-latest --prompt "what files matter most in this repo?"
```

Start the REPL:

```bash
uv run nexus --provider mistral --model mistral-small-latest
```

Run tests after changes:

```bash
uv run --group dev python -m pytest -q
```

## Troubleshooting

### Missing API key

If `MISTRAL_API_KEY` is not set, requests will fail at the provider boundary with an authentication error from the remote API.

### Wrong base URL

If you are routing through a proxy or gateway, set one of:

- `api_base_url` in config
- `AGENT_API_BASE_URL`
- `MISTRAL_BASE_URL`

### Provider selection

Make sure one of these is true:

- `provider = "mistral"` is set in config
- or you pass `--provider mistral`

## Validation

Known working command pattern for this repo:

```bash
uv sync --group dev
uv run --group dev python -m pytest -q
```

