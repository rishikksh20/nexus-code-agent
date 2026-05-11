# 08 — Skills: On-Demand Knowledge Packs

## Prerequisites

Complete [07-permissions.md](07-permissions.md) first.

Your agent now has a rich base: session persistence, layered context, memory, hooks, and permissions. But there is still a problem: as you want the agent to handle more specialized workflows — debugging, code review, writing commit messages, security audits — the base prompt gets crowded and unfocused.

Skills solve this by keeping **specialized instructions out of the base prompt** and loading them only when they are relevant.

---

## What you will build

```
agent/
    skills.py       ← NEW: Skill dataclass, SkillRegistry, load from disk
    tools.py        ← updated: SkillTool
    prompts.py      ← updated: advertise available skills in prompt
skills/             ← NEW: skill files on disk
    debug/
        SKILL.md
    review/
        SKILL.md
    commit/
        SKILL.md
```

---

## 1. What a skill is (and is not)

A **skill** is a named, on-demand instruction document for a specific workflow:

```
base prompt:              always loaded — short, role-defining
skill (debug):            loaded on request — detailed debugging process
skill (code-review):      loaded on request — review checklist
skill (commit):           loaded on request — commit message format
```

A skill is **not**:
- a tool (skills don't execute code)
- a memory entry (skills are developer-authored, not agent-generated)
- part of the base prompt (they stay out until requested)

The model discovers skills through a short summary in the base prompt ('Skills available: debug, review, commit'), then fetches the full content via the `skill` tool when it needs it.

---

## 2. Create the skill files

```bash
mkdir -p skills/debug skills/review skills/commit
```

```markdown
<!-- skills/debug/SKILL.md -->
---
name: debug
description: Diagnose and fix bugs step-by-step without rushing to edit code.
tags: [debugging, troubleshooting, testing]
---

# Debug Skill

Follow this process in order:

1. **Reproduce first.** Ask the user to confirm you can reproduce the issue before proceeding.
2. **Read before editing.** Use read_file and glob to understand the failing code.
3. **Find the smallest failing boundary.** Narrow the problem to one function or module.
4. **Hypothesize, then verify.** State your hypothesis, check it with the code — don't assume.
5. **Make the minimal fix.** Change as little code as possible.
6. **Re-run validation.** Confirm the fix works before calling it done.
7. **Report what changed and why.**

Do not skip steps. Skipping "reproduce" leads to fixing the wrong thing.
```

```markdown
<!-- skills/review/SKILL.md -->
---
name: review
description: Review code for correctness, style, security, and maintainability.
tags: [review, code-quality, security]
---

# Code Review Skill

Review against these criteria in order:

1. **Correctness** — does it do what it claims? Are edge cases handled?
2. **Security** — any injection risks, unsafe deserialization, exposed secrets?
3. **Style** — consistent with project conventions (check memory and project notes)?
4. **Error handling** — are errors caught and logged appropriately?
5. **Tests** — is there test coverage? Are the tests meaningful?
6. **Clarity** — would a new contributor understand this in 60 seconds?

Produce a structured report: summary, issues found (with severity), suggestions.
```

```markdown
<!-- skills/commit/SKILL.md -->
---
name: commit
description: Write a clear, conventional commit message for staged changes.
tags: [git, commit, changelog]
---

# Commit Message Skill

Use the Conventional Commits format:

```
<type>(<scope>): <short summary>

<optional body>

<optional footer>
```

Types: feat, fix, docs, style, refactor, test, chore

Rules:
- Summary line max 72 characters, imperative mood ("add feature", not "added feature")
- Body explains *why*, not *what* (the diff shows what)
- Reference issues in footer: "Closes #42"

Before writing the message:
1. Run glob to list changed files
2. Run read_file on key changed files  
3. Understand the intent of the changes
4. Write the message
```

---

## 3. Create `agent/skills.py`

```python
# agent/skills.py

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Skill:
    """
    One on-demand instruction document.

    name        — identifier used in the `skill` tool call
    description — one-line summary advertised in the prompt
    content     — the full instruction text (often Markdown)
    tags        — keywords for retrieval
    source      — where the skill came from (path or "builtin")
    """
    name: str
    description: str
    content: str
    tags: list[str] = field(default_factory=list)
    source: str = "user"


class SkillRegistry:
    """
    Catalog of available skills.

    Skills can be registered programmatically or loaded from a directory.
    The model sees skills through the `skill` tool and the prompt summary.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def names(self) -> list[str]:
        return list(self._skills.keys())

    def summary(self) -> str:
        """
        One-line summary of all skills for prompt injection.

        Example output:
            Skills available: debug, review, commit
            Use the 'skill' tool to load full instructions for a skill.
        """
        if not self._skills:
            return ""
        names = ", ".join(self.names())
        descriptions = "\n".join(
            f"  - {s.name}: {s.description}"
            for s in self._skills.values()
        )
        return (
            f"Skills available: {names}\n"
            f"{descriptions}\n"
            f"Use the 'skill' tool with the skill name to load full instructions."
        )


def load_skills_from_dir(skills_dir: Path) -> SkillRegistry:
    """
    Discover and load all skills from a directory.

    Expected layout:
        skills/
            debug/
                SKILL.md
            review/
                SKILL.md

    The SKILL.md file may start with a YAML-like frontmatter block:
        ---
        name: debug
        description: ...
        tags: [debugging, testing]
        ---
        <content>
    """
    registry = SkillRegistry()

    if not skills_dir.exists():
        return registry

    for skill_file in skills_dir.rglob("SKILL.md"):
        skill = _parse_skill_file(skill_file)
        if skill:
            registry.register(skill)

    return registry


def _parse_skill_file(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    name = path.parent.name           # directory name as default
    description = ""
    tags: list[str] = []
    content = text

    # Parse optional frontmatter block
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            front = parts[1]
            content = parts[2].strip()

            for line in front.splitlines():
                if line.startswith("name:"):
                    name = line.removeprefix("name:").strip()
                elif line.startswith("description:"):
                    description = line.removeprefix("description:").strip()
                elif line.startswith("tags:"):
                    tag_str = line.removeprefix("tags:").strip().strip("[]")
                    tags = [t.strip() for t in tag_str.split(",") if t.strip()]

    if not description:
        # Use first non-empty line of content as description
        for line in content.splitlines():
            if line.strip() and not line.startswith("#"):
                description = line.strip()[:80]
                break

    return Skill(
        name=name,
        description=description,
        content=content,
        tags=tags,
        source=str(path),
    )
```

---

## 4. Add `SkillTool` to `agent/tools.py`

```python
# agent/tools.py  — add SkillTool

from agent.skills import SkillRegistry


class SkillTool(BaseTool):
    """
    Lets the model load full skill instructions on demand.

    This is the bridge between the prompt summary (which says skills exist)
    and the full skill content (which is only loaded when needed).
    """
    name = "skill"
    description = (
        "Load the full instructions for a named skill. "
        "Call this when you need detailed guidance for a specific workflow."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name to load (e.g. 'debug', 'review', 'commit').",
            }
        },
        "required": ["name"],
    }
    is_mutating = False

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        skill_name = arguments.get("name", "").strip()
        if not skill_name:
            return ToolResult(output="Error: 'name' argument is required.", is_error=True)

        skill = self._registry.get(skill_name)
        if skill is None:
            available = ", ".join(self._registry.names()) or "(none)"
            return ToolResult(
                output=f"Skill '{skill_name}' not found. Available: {available}",
                is_error=True,
            )

        return ToolResult(
            output=skill.content,
            metadata={"skill_name": skill.name, "tags": skill.tags},
        )
```

---

## 5. Advertise skills in the context builder

Update `agent/prompts.py` to add a skills section:

```python
# agent/prompts.py  — add add_skills() to ContextBuilder

class ContextBuilder:
    # ...existing methods...

    def add_skills(self, skills_summary: str) -> "ContextBuilder":
        """
        Include a short summary of available skills.

        This advertises that skills exist WITHOUT loading their full content.
        Full content is only loaded when the model calls the 'skill' tool.
        """
        if skills_summary and skills_summary.strip():
            self._sections.append(f"# Available Skills\n{skills_summary.strip()}")
        return self
```

Update `build_runtime_prompt`:

```python
def build_runtime_prompt(
    *,
    cwd: str,
    tool_names: list[str],
    project_notes: str = "",
    carry_over: dict[str, Any] | None = None,
    user_text: str = "",
    memory_text: str = "",
    skills_summary: str = "",            # ← new
    base_prompt: str = DEFAULT_BASE_PROMPT,
) -> str:
    builder = ContextBuilder(cwd=cwd)
    builder.add_base(base_prompt)
    builder.add_environment()
    builder.add_tools(tool_names)

    if skills_summary:
        builder.add_skills(skills_summary)    # ← new

    if project_notes:
        builder.add_project_notes(project_notes)
    if memory_text:
        builder.add_memory(memory_text)
    if carry_over:
        builder.add_task_focus(carry_over)
    if user_text:
        builder.add_user_goal(user_text)

    return builder.build()
```

---

## 6. Update `Agent` with skills support

```python
# agent/agent.py  — add skill_registry parameter

from agent.skills import SkillRegistry

class Agent:
    def __init__(
        self,
        # ...existing params...
        skill_registry: SkillRegistry | None = None,    # ← new
    ) -> None:
        # ...existing init...
        self.skill_registry = skill_registry

    def _build_system_prompt(self, user_text: str = "") -> str:
        carry_over = self._snapshot.carry_over if self._snapshot else {}
        memory_text = ""
        if self.memory_store and user_text:
            memory_text = self.memory_store.retrieve(user_text, max_entries=3)

        skills_summary = ""
        if self.skill_registry:
            skills_summary = self.skill_registry.summary()

        return build_runtime_prompt(
            cwd=self.cwd,
            tool_names=self.tool_registry.names(),
            project_notes=self.project_notes,
            carry_over=carry_over,
            user_text=user_text,
            memory_text=memory_text,
            skills_summary=skills_summary,       # ← new
            base_prompt=self.base_prompt,
        )
```

---

## 7. Update `main.py`

```python
# main.py  — updated build_agent()

from agent.skills import load_skills_from_dir
from pathlib import Path

def build_agent(project_notes: str = "") -> Agent:
    # ...existing setup...

    skill_registry = load_skills_from_dir(Path("skills"))

    # Register SkillTool only if skills exist
    if skill_registry.names():
        registry.register(SkillTool(skill_registry))

    return Agent(
        # ...existing args...
        skill_registry=skill_registry,
    )
```

---

## 8. Run and test skills

```bash
python main.py
```

The model's prompt now includes:

```
# Available Skills
Skills available: debug, review, commit
  - debug: Diagnose and fix bugs step-by-step without rushing to edit code.
  - review: Review code for correctness, style, security, and maintainability.
  - commit: Write a clear, conventional commit message for staged changes.
Use the 'skill' tool with the skill name to load full instructions.
```

With a real LLM:
```
you> review the code in src/auth.py
  · Thinking... (turn 1)
  ⚙ skill(name='review')
  ✓ skill → # Code Review Skill↵Review against these criteria...
  ⚙ read_file(file_path='src/auth.py')
  ✓ read_file → ...

agent> Code Review: src/auth.py
  1. Correctness: ...
  2. Security: Found potential SQL injection in line 47...
```

The model loaded the skill, then used it to structure its review — without you embedding the review instructions in the base prompt.

---

## 9. Skill design rules

| Good skill topic | Bad skill topic |
|---|---|
| A repeatable workflow (debug, review, deploy) | A one-off fact (the server IP is 10.0.0.1) |
| A process with steps | A random definition |
| Something the model should follow procedurally | Something the model already knows well |
| Domain-specific expertise (security audit) | Generic advice (be careful) |

A skill should teach a **process** to follow, not just a definition to recall. Definitions belong in memory. Processes belong in skills.

---

## 10. Exercises

**Exercise A — Skill for your domain**

Write a `SKILL.md` for a workflow specific to your own project. Examples: `deploy.md` for deployment steps, `migration.md` for database migrations, `test-strategy.md` for your testing approach.

**Exercise B — Skill tags in retrieval**

Update `SkillTool.execute()` to also accept an optional `tags: list[str]` argument. Return all skills that have at least one matching tag, instead of requiring an exact name match.

**Exercise C — Auto-suggest skill**

Create a `SuggestSkillHook` for `HookEvent.USER_PROMPT_SUBMIT`. If the user's prompt contains words like "debug", "review", "commit", "refactor" and a matching skill exists, emit a `HookResult.emit()` message: `"[hint] The '{name}' skill may help with this. The model will load it if needed."`.

---

## 11. Checklist before moving on

- [ ] `Skill` dataclass has name, description, content, tags, source
- [ ] `SkillRegistry` supports `register()`, `get()`, `list_skills()`, `summary()`
- [ ] `load_skills_from_dir()` discovers `SKILL.md` files recursively
- [ ] Frontmatter (`name:`, `description:`, `tags:`) is parsed from `SKILL.md`
- [ ] `SkillTool` is registered only if skills directory has entries
- [ ] `ContextBuilder.add_skills()` adds a summary section — NOT the full content
- [ ] Full skill content is only loaded when the model calls the `skill` tool
- [ ] `Agent` receives `skill_registry` and passes the summary to `build_runtime_prompt`
- [ ] At least three `SKILL.md` files exist in `skills/`

---

Next: [09-plan-mode-and-auto-mode.md](09-plan-mode-and-auto-mode.md)

