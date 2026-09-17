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

## Data safety (non-negotiable)

These are **sensitive human spatial datasets** (GDPR / Karolinska). Safety is the
first design constraint, not an afterthought: the agent is built so that raw data
*cannot* reach an LLM server, and the guarantee is enforced mechanically rather
than left to the model's goodwill.

**What the reasoning step is allowed to see — the schema, and only the schema:**
obs column *names*, dtypes, cardinalities (distinct-value counts),
missing/aggregate counts, CLI `--help`, and error text.

**What must never cross the boundary — any data value:** the expression matrix,
cell coordinates, patient identifiers, sample IDs, and the per-column *example
values* that `karospace --inspect-input` prints. The agent reasons about your
experiment from column names, types, and cardinalities alone.

**How each stage enforces it:**

| | Enforcement |
| --- | --- |
| **Stage 1** (Claude Code) | Inspect is piped through `sed 's/ examples:.*//'` so example values are stripped in-shell *before* any output enters the model's context. The skill and subagent are instructed to only ever run it this way. |
| **Stage 2** (Agent SDK) | Enforced in code: all built-in tools are disabled, so the model's only capabilities are seven sanitizing wrappers. It cannot read a file off disk, and `inspect_input` runs `strip_inspect_examples` before returning anything. |

All heavy compute (`karospace`, `karospace-companion`, scanpy, DESeq2) runs
**locally, where the data lives**. Only sanitized schema ever leaves the machine.

## Stage 1 — Claude Code (this repo)

Prove the workflow with zero infrastructure:

- **Skill** `build-karospace-viewer` — the parameter-selection playbook.
- **Subagent** `karospace-viewer-builder` — runs the loop end-to-end.

Usage inside Claude Code:

```
/build-karospace-viewer  ~/data/my_xenium.h5ad "grid by sample, colour by cell_type, focus on Cd4/Cd8a/Gfap"
```

or delegate the whole thing to the subagent.

## Stage 2 — packaged product (`stage2/`)

The proven workflow, graduated to the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk) (Python) as a
hostable app alongside KaroSpaceBuilder. Same architecture, now enforced in
code: **local hands, Claude brain, sanitized metadata only** — the agent runs
with all built-in tools disabled, so its only capabilities are the seven
sanitizing wrappers over the CLIs. It cannot read raw data off disk.

```bash
cd stage2 && pip install -e .
karospace-agent build ~/data/my_xenium.h5ad "grid by sample, colour by cell_type"
```

See [`stage2/README.md`](stage2/README.md) for layout, config, and tests.
