---
name: note-taking
description: Creates or appends time-stamped notes to a `notes.toml` file in the current working directory. Each note is stored under a timestamp section header in TOML format. Use when the user asks to take a note, write a note, save a note, or log something for later reference.
---

# Note Taking

Save notes to a `notes.toml` file in the current working directory, appending each entry under a timestamp section header.

## Process

1. **Check for existing file** — look for `notes.toml` in the current working directory (`pwd`). If it does not exist, create it before writing.

2. **Generate a timestamp** — fetch the current system date and time in the format `YYYY-MM-DD - HH:MM:SS` (24-hour clock).

3. **Append the note** — add the new entry at the end of the file using the structure below. Never overwrite or modify existing entries.

4. **Confirm to the user** — after writing, confirm the note was saved along with the timestamp used.

## TOML Entry Format

Each note must follow this exact structure:

```toml
["YYYY-MM-DD - HH:MM:SS"]
content = """
<note text here>
"""
```

- The timestamp is the TOML table key — wrap it in double quotes so colons and spaces are valid.
- Use triple-quoted strings (`"""`) for `content` to preserve newlines and avoid escaping issues.
- Leave one blank line between consecutive entries to keep the file readable.

## Example

```toml
["2026-05-19 - 14:32:07"]
content = """
Reviewed the agent runtime loop. Need to add retry logic for tool call failures.
"""

["2026-05-19 - 15:10:44"]
content = """
Updated the note-taking skill format to match the new TOML spec.
"""
```

## Rules

- **Append only** — never delete or edit existing notes.
- **One entry per invocation** — write exactly one timestamped block per call.
- **No duplicate timestamps** — if two notes are written within the same second, append a suffix (e.g., `14:32:07-2`) to keep keys unique.
- **Preserve file encoding** — write UTF-8 without BOM.