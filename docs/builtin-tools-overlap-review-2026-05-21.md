# Builtin Tools Overlap Review

Date: 2026-05-21

## Scope

This review covers first-party builtin tools under `nexus/tools/builtin/` and the public builtin registry.

`reference_code/` was intentionally excluded.

## Current Recommendation

The builtin tool surface has been reduced to canonical runtime tools. Legacy compatibility wrappers and import-only aliases are no longer part of the public builtin API.

Keep these tools as first-class builtins:

- File and workspace: `read_file`, `write_file`, `edit`, `insert_edit_into_file`, `apply_patch`, `list_dir`, `glob`, `grep`
- Code understanding: `lsp`, `code_index`, `semantic_search`
- Git inspection: `git_status`, `git_diff`
- Verification and execution: `run_tests`, `run_python_check`, `run_formatter`, `bash`
- State, network, and utility: `memory`, `todos`, `web_fetch`, `web_search`, `get_time`

## Completed Cleanup

- The legacy file-writing wrapper was removed. `write_file` is the only canonical full-file writer.
- The standalone Python reference-search tool was merged into `lsp`; use the `lsp` reference lookup operation for that workflow.
- The duplicate verification tools were replaced by `run_python_check`, which truthfully represents the shared compile/syntax-check behavior.
- Import-only aliases were removed. Use `ListDirTool` and `ShellTool` as Python class names; runtime tool names remain `list_dir` and `bash`.

## Still Separate By Design

The editing tools remain separate because they have different safety and usage contracts:

- `write_file`: create or fully overwrite a file
- `edit`: exact text replacement
- `insert_edit_into_file`: contextual anchor-based edit
- `apply_patch`: unified diff application

The filesystem discovery tools also remain separate:

- `list_dir`: inspect one directory level
- `glob`: match paths
- `grep`: match file contents

`memory` and `todos` remain separate because persistent memory and session-local task tracking have different lifetimes and user intent.

## Future Candidates

The next possible simplification is the Python code-understanding family. `semantic_search` is currently lexical and Python-only, so it may eventually be renamed or folded into a broader code-search surface. `git_status` and `git_diff` could also be merged into a single operation-based `git` tool, but the current split is still clear and low risk.
