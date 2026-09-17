# KaroSpaceAgent

An agent that navigates the end-to-end creation of a
[KaroSpace](../KaroSpace) spatial-transcriptomics viewer from a raw `.h5ad` /
SpatialData `.zarr` file.

It exists because getting from raw data to a good viewer means choosing ~30
correct flags for a messy dataset — the part a GUI can't do for you. The agent
inspects the data's metadata, reasons about the right parameters (which column is
the section key, which is the main annotation, whether pseudobulk makes sense for
the experimental design, …), optionally enriches the file with
[KaroSpaceCompanion](../KaroSpaceCompanion), runs the export, and validates the
result.

## Stage 1 — Claude Code (this repo)

Prove the workflow with zero infrastructure:

- **Skill** `build-karospace-viewer` — the parameter-selection playbook.
- **Subagent** `karospace-viewer-builder` — runs the loop end-to-end.

Usage inside Claude Code:

```
/build-karospace-viewer  ~/data/my_xenium.h5ad "grid by sample, colour by cell_type, focus on Cd4/Cd8a/Gfap"
```

or delegate the whole thing to the subagent.

## Stage 2 — packaged product (later)

Graduate to the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk)
(Python) once the workflow is proven, for something hostable alongside
KaroSpaceBuilder. Keep the same architecture: **local hands, Claude brain,
sanitized metadata only.**

## Boundaries

- Compute runs locally, where the data lives.
- Only sanitized metadata (column names, dtypes, example values, errors) is used
  for reasoning — never the expression matrix or patient identifiers.
