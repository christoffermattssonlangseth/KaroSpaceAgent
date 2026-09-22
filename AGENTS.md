# KaroSpaceAgent

This repository contains a standalone agent app and workflows for building
KaroSpace spatial-transcriptomics viewers. The viewer and companion pipeline
live in sibling repositories, `../KaroSpace` and `../KaroSpaceCompanion`.

## Viewer-building tasks

Use the `build-karospace-viewer` skill in
`.agents/skills/build-karospace-viewer/SKILL.md`. It shares its source with the
Claude Code skill in `.claude/skills/build-karospace-viewer/SKILL.md`.
Resolve the playbook's `scripts/` paths from this repository root, even when
Codex was opened in a subdirectory. Check the installed CLI's `--help` before
using unfamiliar flags.

When the `karospace` MCP server is connected, prefer its tools for dataset
operations. They use the standalone app's sanitizing wrappers without starting
a Claude session. The skill maps workflow steps to tool names. Use the shell
fallback only when the corresponding MCP tool is unavailable.

Keep all dataset computation local. Only schema may enter model context:
column names, dtypes, cardinalities, missing/aggregate counts, CLI help, and
error text. Never read dataset values, expression matrices, coordinates,
patient/sample identifiers, or inspect example values into context. Always
strip inspect examples before capturing output:

```bash
karospace <input.h5ad> --inspect-input | sed 's/ examples:.*//'
```

Use `scripts/inspect_structure.py` for the additional schema probe. Validate
artifacts using file metadata; do not read generated viewers or sidecars into
context because they contain data values. Review the chosen flags against the
skill's checklist before reporting success.

## App development

The standalone Python app is in `app/`; see `app/README.md` for setup and tests.
Its Claude Agent SDK integration restricts the model to sanitizing tools.
The Codex app-server backend attaches no execution environment and dispatches
only registered KaroSpace tools through local workers.
Preserve that boundary when modifying the app. Repository skills run inside
the host coding agent and rely on these instructions; they do not provide the
app's enforced tool restriction.
