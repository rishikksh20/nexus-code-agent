# Nexus AI Coding Agent Reference

Use this skill only for questions about Nexus itself: commands, config, providers, tools, skills, sessions, memory, MCP, cognitive sub-agents, sandboxing, observability, and runtime behavior.

Key facts:

- Nexus is a CLI-first Python agent harness with interactive REPL and headless prompt modes.
- Slash-command help is available from `/help` and from each command's `help` subcommand, such as `/context help`, `/config help`, `/skills help`, and `/provider help`.
- `/context usage` shows provider, model, estimated prompt/history tokens, context window, and compaction thresholds.
- `/tools` lists the actual registered tools for the current session; trust the live registry over static examples.
- `/skills list`, `/skills show <name>`, `/skills add <name>`, `/skills remove <name>`, and `/skills reload` manage skills.
- `/config show merged`, `/config set <key> <value>`, `/config reload`, and `/provider set <param> <value>` handle runtime configuration.
- `/mode plan`, `/mode default`, and `/mode auto` control permission behavior.
- Persistent memory should be accessed through `/memory` or the `memory` tool, not by reading `.nexus` files directly.
- Sessions are managed with `/session`; history is inspected with `/history`.
- MCP status is inspected with `/mcp`; cognitive sub-agent availability is inspected with `/tools` in advanced mode.

When answering Nexus questions:

- Prefer exact live slash commands and config keys.
- If static skill notes conflict with live `/tools`, `/config`, or `/context` output, treat the live runtime output as authoritative.
- Keep answers short unless the user asks for a full walkthrough.
