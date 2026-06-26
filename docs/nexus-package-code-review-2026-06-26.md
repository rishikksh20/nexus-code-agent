# Nexus Package Code Review - 2026-06-26

## Scope

This review covers the live `nexus/` package only. It intentionally excludes
`reference_code/`, `workspace/`, and generated/runtime state.

The review focused on loopholes, redundant code paths, duplicated logic,
security-sensitive behavior, and correctness risks in the AI coding agent
runtime.

## Method

- Mapped every Python file under `nexus/`.
- Identified large modules and large functions with an AST scan.
- Searched for duplicated functions, legacy compatibility surfaces, policy
  checks, approval flow, path enforcement, tool schema handling, and provider
  streaming/retry logic.
- Cross-checked prior review documents in `docs/` so this report focuses on
  current code and still-open risks.

## Executive Summary

Nexus has several strong architectural boundaries: approval prompting is still
centralized through `run_agent_turn()`, `Agent.run()` remains event-driven,
provider-safe message history ordering is actively protected, and MCP tools are
treated as mutating by default unless metadata proves otherwise.

The biggest current risks are concentrated in the tool execution and approval
pipeline. `--auto-confirm` currently suppresses confirmation even for
high/dangerous shell commands, turn-wide approval is broader than its policy
description implies, and the resume path for approved tool calls bypasses the
per-turn tool-call budget. These are high-priority because they affect the
security contract of an agent that can execute commands and mutate files.

The largest maintainability issue is duplication across three tool execution
paths in `nexus/runtime/agent.py`. The duplicated branches have already drifted
into real behavior differences. A shared preparation/execution pipeline would
reduce both complexity and security risk.

## High Severity Findings

### 1. `--auto-confirm` can bypass high/dangerous shell confirmation

**Files**

- `nexus/cli/args.py:42`
- `nexus/app.py:131-137`
- `nexus/security/permissions.py:169-180`
- `nexus/runtime/agent.py:320-323`
- `nexus/runtime/agent.py:1136-1139`
- `nexus/runtime/agent.py:1732-1735`
- `nexus/runtime/turn_runner.py:191-199`
- `nexus/runtime/turn_runner.py:524-530`

**Evidence**

`--auto-confirm` is documented as automatically confirming all mutating tool
calls. The permission checker separately documents that high/dangerous bash
commands should always require confirmation, even in automatic modes.

However, each major execution path skips confirmation when `auto_confirm=True`:

- approved-call resume path in `_execute_approved_tool_calls()`
- serial execution path in `_agentic_loop()`
- parallel first-batch preparation in `_prepare_parallel_first_tool_call()`

The turn runner also treats any remaining confirmation under `auto_confirm` as
approved.

**Impact**

A user who enables `--auto-confirm` can unintentionally allow high-risk shell
commands without the explicit checkpoint the permission policy says should
remain in place. This weakens one of the most important safety boundaries in
the agent.

**Recommendation**

Make `auto_confirm` apply only to confirmations that are explicitly eligible for
automatic approval. A simple fix is to attach an `auto_confirmable` boolean to
`PermissionDecision.CONFIRM` decisions, default it to `False` for high/dangerous
bash, and require all execution paths to honor it through one shared helper.

### 2. Turn-wide approval is too broad and ignores tool, path, and risk

**Files**

- `nexus/runtime/turn_runner.py:711-721`
- `nexus/runtime/turn_runner.py:735-737`
- `nexus/security/manager.py:171-180`
- `nexus/runtime/agent.py:2208-2223`

**Evidence**

`supports_turn_wide_approval()` currently always returns `True`. When the user
chooses approval for the whole turn, `record_approval_response()` records a
turn-wide mutating approval.

`ApprovalManager.is_turn_wide_mutating_preapproved()` accepts `tool_name`,
`is_mutating`, and `risk_level`, but ignores all three and returns the single
turn-wide flag. `_is_tool_preapproved()` then applies that broad approval to
confirmable tool calls.

**Impact**

After approving one operation for the turn, later operations with different
tools, paths, or higher shell risk can inherit the same approval. This conflicts
with the intended policy that high/dangerous bash should stay outside broad
turn-wide approval.

**Recommendation**

Narrow turn-wide approval by risk and tool class. At minimum:

- never apply it to high/dangerous bash
- apply it only to mutating tools that match the approved risk class
- consider storing approved path scopes for file mutations
- make `supports_turn_wide_approval()` inspect the current pending call instead
  of returning `True`

### 3. Approval resume path bypasses `max_tool_calls_per_turn`

**Files**

- `nexus/runtime/agent.py:152-162`
- `nexus/runtime/agent.py:190-201`
- `nexus/runtime/agent.py:944-954`
- `nexus/runtime/agent.py:1359-1388`
- `nexus/runtime/turn_runner.py:444-480`
- `nexus/runtime/turn_runner.py:773-784`

**Evidence**

The normal serial and parallel paths enforce `max_tool_calls_per_turn`.
The resume path is different:

- `Agent.run()` immediately delegates `resume_tool_calls` to
  `_execute_approved_tool_calls()`
- `_execute_approved_tool_calls()` loops over every resumed call without a
  budget check
- `_pending_tool_calls_from_confirmation_model_response()` returns the entire
  suffix of the model's tool-call batch after the approved call
- `_resume_approved_tool_calls()` passes that tuple back to `agent.run()`

**Impact**

If a model emits a large batch of tool calls and the first one requires
approval, approving it can resume the remaining calls without applying the
configured per-turn limit.

**Recommendation**

Pass the remaining turn budget into the resume path and enforce it in the same
helper used by serial and parallel execution. The resume path should not be a
separate execution policy.

## Medium Severity Findings

### 4. Interpreter shell commands are under-classified as low risk

**Files**

- `nexus/tools/builtin/shell.py:60-99`
- `nexus/security/classifier.py:41-78`
- `nexus/security/permissions.py:203-207`
- `nexus/tools/builtin/shell.py:203-214`

**Evidence**

`classify_bash_risk()` treats base commands such as `python`, `python3`,
`node`, `ruby`, and `perl` as low risk. It does not distinguish harmless
version checks from arbitrary code execution such as `python -c ...` or
`node -e ...`.

`CommandClassifier._SAFE_PATTERNS` duplicates a similar safe list and includes
interpreter prefixes. `CommandClassifier.is_safe()` appears unused, while
`classify()` delegates to `classify_bash_risk()`.

Default-mode permissions allow low-risk bash commands.

The workspace `cwd` escape check in `shell.py:203-214` is good and appears to
address a previous shell working-directory loophole.

**Impact**

Interpreter one-liners can execute arbitrary code while being classified as
low-risk shell commands.

**Recommendation**

Classify interpreter invocations with code execution flags or script paths as
at least medium risk. Keep low risk for narrow, read-only forms such as
`python --version`, `python -m pytest --collect-only`, or other explicitly
approved patterns.

### 5. Tool schema validation is incomplete and only required fields are checked

**Files**

- `nexus/tools/base.py:119-125`
- `nexus/runtime/agent.py:1911-1928`
- `nexus/runtime/agent.py:2414-2415`
- `nexus/tools/builtin/patch.py:251-260`
- `nexus/tools/builtin/patch.py:277`
- `nexus/tools/builtin/list_dir.py:74-75`
- `nexus/tools/builtin/edit_file.py:103-106`
- `nexus/tools/builtin/shell.py:201`

**Evidence**

`Tool.validate_params()` says JSON Schema validation is done upstream, but the
agent currently checks only missing required fields through
`_missing_required_fields()`. It does not validate scalar types, numeric ranges,
additional properties, enum values, or common coercion hazards.

Examples:

- `apply_patch.strip` declares an integer with a minimum, but execution casts
  with `int(...)`
- `_affected_file_paths()` casts `strip` before tool execution, so a malformed
  value can fail outside the normal tool result path
- boolean fields in tools use `bool(value)`, so a string such as `"false"` is
  treated as `True`
- shell timeout uses `int(...)`; the schema maximum is not enforced centrally

**Impact**

Malformed model arguments can produce surprising behavior, uncaught exceptions,
or bypass intended schema constraints.

**Recommendation**

Add central schema validation before permission checks and execution. A compact
internal validator covering object type, required fields, enums, scalar types,
numeric bounds, arrays, and `additionalProperties` would cover the current
schemas. Alternatively, add `jsonschema` if dependency policy allows it.

### 6. Provider streaming and retry logic is duplicated and has drift

**Files**

- `nexus/integrations/openai_compatible.py:162-174`
- `nexus/integrations/openai_compatible.py:321-405`
- `nexus/integrations/openai_compatible.py:640-650`
- `nexus/integrations/cohere.py:141-153`
- `nexus/integrations/cohere.py:324-390`
- `nexus/integrations/cohere.py:593-603`
- `nexus/integrations/ollama.py:277-395`
- `nexus/integrations/retry.py`

**Evidence**

An AST duplicate scan found identical `_retry_after_seconds()` implementations
in the OpenAI-compatible and Cohere providers. Retryable provider error classes
also duplicate the same shape.

Streaming implementations have drifted:

- OpenAI-compatible and Ollama stream readers use bounded queues,
  cancellation events, and response close/finally cleanup
- Cohere uses an unbounded queue and has less cancellation/close handling in
  its stream worker path

`nexus/integrations/retry.py` already centralizes retry helpers and is a natural
home for shared retry-after parsing.

**Impact**

Provider behavior diverges in subtle ways. Streaming cancellation, memory
pressure, and retry behavior are harder to reason about consistently.

**Recommendation**

Move retry-after parsing and retryable error plumbing into shared provider
helpers. Consider extracting a shared streaming bridge for blocking SDK streams
that standardizes bounded queues, cancellation, response closing, and exception
propagation.

### 7. Slash command config writer can discard dict-style `[sub-agents]` layout

**Files**

- `nexus/config/loader.py:340-354`
- `nexus/runtime/slash_commands.py:1764-1784`
- `nexus/runtime/slash_commands.py:2147-2173`
- `nexus/runtime/slash_commands.py:2183-2185`
- `nexus/config/editor.py:50-59`

**Evidence**

The config loader accepts dict-style sub-agent entries. The slash command
payload helper only preserves list-style `sub-agents` and legacy
`subagent_profiles`. If `payload["sub-agents"]` is a dict, it is replaced with a
new list when `/sub-agent allow` or `/sub-agent disallow` writes config.

The slash command writer also serializes TOML itself. The config editor module
already contains tomlkit-based helpers for preserving top-level layout and
comments.

**Impact**

Using slash commands to update sub-agent permissions can lose existing named
dict-style sub-agent configuration and rewrite local config formatting.

**Recommendation**

Route slash command config mutation through `nexus/config/editor.py` or extend
the payload writer to preserve dict-style sections. Add a regression test with
dict-style `[sub-agents.<name>]` entries.

### 8. Tool execution pipeline has three mostly copied paths

**Files**

- `nexus/runtime/agent.py:190-348`
- `nexus/runtime/agent.py:944-1170`
- `nexus/runtime/agent.py:1249-1358`
- `nexus/runtime/agent.py:1359-1760`

**Evidence**

The following paths repeat substantial logic:

- approved-call resume path
- serial tool execution path
- parallel first-batch preparation/execution path

They each handle overlapping pieces of:

- tool record lookup
- unavailable tool results
- required argument checks
- user-input interruption
- confirmation decisions
- permission checks
- refusal handling
- duplicate mutation detection
- execution and post-mutation refresh

There are also two execution helpers, `_execute_tool_call()` and
`_run_tool_call()`, with overlapping responsibilities.

**Impact**

Policy changes must be replicated across multiple branches. The current
auto-confirm and resume-budget issues are examples of this duplication turning
into behavioral drift.

**Recommendation**

Introduce a single tool-call preparation result, for example:

- unavailable
- invalid_arguments
- needs_user_input
- needs_confirmation
- permission_denied
- duplicate_mutation
- ready_to_execute

Then use one executor for resume, serial, and parallel paths. Keep only the
scheduling difference outside the shared policy path.

### 9. Write-path policy is duplicated and partly name-based

**Files**

- `nexus/security/permissions.py:210-218`
- `nexus/tools/builtin/write_file.py:80-103`
- `nexus/tools/builtin/smart_edit.py:211-232`
- `nexus/tools/builtin/patch.py:198-208`
- `nexus/tools/builtin/patch.py:299-302`
- `nexus/tools/filesystem.py:146-155`

**Evidence**

`PermissionChecker._path_policy()` hard-codes a set of write tool names.
Individual write tools also enforce workspace and `.nexus` restrictions
internally. `apply_patch` is not in the permission checker's write-tool set, but
it performs its own workspace checks.

This is not currently a direct bypass for built-in tools because several tools
have internal checks. The risk is fragmentation: new write-capable tools can
miss one layer and still behave differently in approval, denial, or audit.

**Impact**

Path policy is harder to keep consistent across built-in tools, plugin tools,
and MCP tools.

**Recommendation**

Move write-boundary calculation behind a shared tool metadata protocol or helper
that reports affected paths and enforces workspace restrictions consistently.
Use that helper from both permissions and tool implementations.

### 10. `insert_edit_into_file` approval preview can differ from final mutation

**Files**

- `nexus/tools/builtin/smart_edit.py:180-196`
- `nexus/tools/builtin/smart_edit.py:260-273`

**Evidence**

`get_confirmation()` previews a diff from the current file to the raw `code`
argument. Execution then transforms the edit through marker parsing or fuzzy
replacement. If no markers are present and fuzzy replacement fails, the tool
appends the new code to the end of the file.

**Impact**

The user may approve a preview that is not the actual mutation applied to disk.
This is risky for a smart edit tool because approvals should describe the final
write exactly.

**Recommendation**

Compute the final candidate content once, use it for the confirmation diff, and
then write that exact content after approval. Avoid append-on-failure unless the
user explicitly requested an insertion.

### 11. `apply_patch` is not transactional across files

**Files**

- `nexus/tools/builtin/patch.py:290-387`

**Evidence**

`ApplyPatchTool.execute()` applies each file operation as it iterates:

- creates files immediately
- deletes files immediately
- writes modified files immediately

If a later file or hunk fails, earlier mutations remain on disk while the tool
returns an error result.

`ApplyPatchTool` also lacks a `get_confirmation()` method, so approvals do not
receive a purpose-built patch preview from the tool itself.

**Impact**

A failed patch can leave the workspace in a partially mutated state. This is
especially problematic when the patch spans several files.

**Recommendation**

Add a dry-run phase that validates every file operation and hunk before writing
anything. Then apply all writes after validation. If partial application remains
intentional, return explicit partial-mutation metadata and surface it clearly in
the UI.

### 12. Workspace plugins execute arbitrary Python at startup by default

**Files**

- `nexus/extensions/plugins.py:64-80`
- `nexus/extensions/plugins.py:86-115`
- `nexus/app.py:108`
- `nexus/app.py:216-223`

**Evidence**

Plugin roots include global plugin directories, workspace `.nexus/plugins`, and
workspace `.agents/tools`. Plugin loading imports and executes Python modules
before inspecting their `register()` function.

`NexusApp.initialize()` defaults `load_plugins=True`, and normal app startup
loads plugins unless disabled.

**Impact**

Opening an untrusted workspace can execute arbitrary Python from workspace
plugin locations before the user has interacted with the agent. This may be an
accepted extensibility tradeoff, but it should be treated as a security boundary
and documented accordingly.

**Recommendation**

Consider one or more mitigations:

- default-disable workspace plugin roots in untrusted directories
- require explicit trust before loading workspace plugins
- show a startup warning listing plugin files that will execute
- separate global trusted plugins from workspace-local plugins

## Low Severity Findings

### 13. Legacy compatibility tools remain as cleanup debt

**Files**

- `nexus/tools/filesystem.py:26-111`
- `nexus/tools/registry.py:43-68`

**Evidence**

`ModifyFileTool` and `ReplaceTextTool` still exist for compatibility. They are
not in the default core registry, which currently uses the canonical tools.

**Impact**

The code is not part of the default surface, but it increases maintenance load
and can confuse future examples or plugin integrations.

**Recommendation**

Keep them out of the default registry. If they are no longer needed, plan a
deprecation/removal path. If they must remain, mark them clearly as legacy in
docstrings and tests.

### 14. Duplicate and unused shell safety classifier can drift

**Files**

- `nexus/security/classifier.py:41-78`
- `nexus/tools/builtin/shell.py:60-99`

**Evidence**

`CommandClassifier._SAFE_PATTERNS` and `CommandClassifier.is_safe()` appear to
be unused, while actual classification delegates to `classify_bash_risk()`.
Both places encode overlapping ideas about safe commands.

**Impact**

Two independent safety lists can diverge. The unused one already contains broad
interpreter prefixes that would be unsafe if used directly for permission.

**Recommendation**

Delete the unused `is_safe()` path or make it delegate to the single canonical
classifier. Keep all command risk classification in one module.

## Redundancy and Duplication Inventory

### Large functions worth splitting

The largest functions are where most policy drift is likely to happen:

- `nexus/runtime/agent.py::_agentic_loop` - about 695 lines
- `nexus/sandbox/agent_tool.py::_execute_direct` - about 393 lines
- `nexus/ui/textual_rendering.py::render_event` - about 300 lines
- `nexus/runtime/turn_runner.py::run_agent_turn` - about 272 lines
- `nexus/config/loader.py::_validate_config_values` - about 220 lines

These are not automatically bugs, but they are the best candidates for focused
extraction because they combine control flow, policy decisions, formatting, and
error handling.

### Execution-path duplication

The most important duplication is in `nexus/runtime/agent.py`, where resume,
serial, and parallel tool execution repeat policy checks. This should be treated
as the highest-value refactor because it directly affects approval safety.

### Provider duplication

Provider modules duplicate retry-after parsing, retryable error wrapping, and
thread-to-async streaming patterns. This should be consolidated after the
approval pipeline, because it is less immediately security-critical but still
important for runtime reliability.

### Config writing duplication

Config reading and upgrade logic are correctly centralized under
`nexus/config/`, but slash command writes still perform their own TOML
serialization. This is a maintainability gap and can cause format/data loss.

### Tool/path policy duplication

Write boundary checks live in both permission policy and individual tools.
Centralizing affected-path calculation would make file mutation approval,
denial, and auditing more consistent.

## Positive Notes

- Approval callbacks are centralized in `run_agent_turn()`, preserving the
  intended runtime boundary.
- `Agent.run()` remains event-driven and uses `resume_tool_calls` instead of
  embedding user-facing approval callbacks.
- Provider-safe history ordering is actively guarded. Assistant messages with
  unresolved tool calls are skipped or stripped before persistence/model reuse.
- MCP tool mutation classification is conservative by default.
- Shell `cwd` resolution now enforces workspace containment before execution.
- Default core registry uses canonical tools and does not re-add the legacy
  `modify_file` or `replace_text` tools.

## Suggested Remediation Order

1. Fix `--auto-confirm` so high/dangerous bash confirmation cannot be skipped.
2. Restrict turn-wide approval by risk/tool/path and exclude high/dangerous bash.
3. Apply `max_tool_calls_per_turn` to the approval resume path.
4. Introduce central tool argument schema validation.
5. Refactor `nexus/runtime/agent.py` tool-call preparation into one shared path.
6. Tighten shell interpreter risk classification.
7. Make `apply_patch` transactional and align smart-edit previews with final
   mutations.
8. Preserve dict-style sub-agent config when slash commands write TOML.
9. Consolidate provider retry/streaming helpers.
10. Decide whether workspace plugin auto-loading should require explicit trust.

## Verification

This was a static review of the `nexus/` package and related documentation.
`uv run python -m compileall -q nexus` passed.
No production code was changed by this review.
