# Nexus Skills

Nexus skills follow the Agent Skills directory shape: a skill is a directory
with a required `SKILL.md` file. Nexus loads skill metadata at startup and only
injects full instructions for skills that are active.

## Directory Structure

```text
skill-name/
  SKILL.md
  scripts/
  references/
  assets/
```

Only `SKILL.md` is required. `scripts/`, `references/`, and `assets/` are
available as relative resources for the agent to inspect when needed; Nexus does
not load them into the prompt automatically.

## SKILL.md Format

`SKILL.md` must start with YAML frontmatter and then Markdown instructions:

```markdown
---
name: code-review
description: Review code changes for bugs, regressions, and missing tests. Use when the user asks for review.
license: MIT
compatibility: Requires git
metadata:
  owner: platform
allowed-tools: read_file grep git_diff
---

# Code Review

Inspect diffs first. Report findings before summaries.
```

Required fields:

- `name`: 1-64 chars, lowercase letters, numbers, hyphens, no leading/trailing or doubled hyphens. Must match the parent directory.
- `description`: 1-1024 chars, describing what the skill does and when to use it.

Optional fields:

- `license`
- `compatibility`: max 500 chars
- `metadata`: string key-value mapping
- `allowed-tools`: experimental space-separated string or list

Nexus does not support skill-defined custom slash commands or `user-invocable`.

## Discovery

Skills are discovered in this order; later roots override earlier roots:

1. Packaged built-ins: `nexus/builtin_skills/`
2. Extra roots from `skill_paths`
3. Global catalogue: `~/.nexus/skills/`
4. Workspace skills: `.nexus/skills/`
5. Standard Agent Skills project path: `.agents/skills/`

Workspace initialization copies missing packaged built-ins into
`.agents/skills/`. Those project copies override the packaged source and are
readable by filesystem tools even when general hidden-path reads are disabled.
Existing workspace copies are preserved unless `nexus init --force` is used.

Invalid skills are skipped with a warning. Heading-only legacy `SKILL.md` files
without frontmatter must be migrated before Nexus will load them.

## Activation

Workspace activation lives in `.nexus/config.toml`:

```toml
skill_paths = []
enabled_skills = ["nexus-agent", "review-*"]
disabled_skills = ["experimental-*"]
```

Patterns support exact names, shell-style globs, and regex with a `re:` prefix.
`enabled_skills` activates matching skills. `disabled_skills` removes matching
skills from the active set. CLI `--skill <name>` activates a skill for that run
only and does not edit config.

Nexus keeps the system prompt lightweight by including skill metadata only:
name, description, source, active state, optional fields, and `SKILL.md` path.
It does not inject full skill bodies into the prompt. Use `/skills show <name>`
or read the listed `SKILL.md` when full instructions are needed.

## REPL Commands

```text
/skills list
/skills available
/skills show <name>
/skills activate <name>
/skills deactivate <name>
/skills create-local <name>
/skills remove-local <name>
/skills reload
```

`activate` and `deactivate` write workspace config, reload skill state, refresh
skill-backed sub-agent tools, and rebuild the cached system prompt. `add` and
`remove` remain compatibility aliases.

## Migration

Old Nexus skills that looked like this are no longer valid:

```markdown
# Review

Always review carefully.
```

Wrap them in Agent Skills frontmatter:

```markdown
---
name: review
description: Review code changes and report correctness issues. Use when the user asks for review.
---

# Review

Always review carefully.
```
