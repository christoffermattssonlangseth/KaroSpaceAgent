# KaroSpaceAgent

A standalone agent that builds a [KaroSpace](../KaroSpace) spatial-transcriptomics
viewer from a raw `.h5ad` / SpatialData `.zarr` file.

```bash
karospace-agent build ~/data/my_xenium.h5ad "grid by sample, colour by cell_type"
```

It exists because getting from raw data to a good viewer means choosing ~30
correct flags for a messy dataset — and *knowing which choices are right* takes
some spatial-transcriptomics expertise. The agent supplies that judgement: it
inspects the data's metadata, reasons about the right parameters (which column is
the section key, which is the main annotation, whether pseudobulk makes sense for
the experimental design, …), optionally enriches the file with
[KaroSpaceCompanion](../KaroSpaceCompanion), runs the export, reads errors and
iterates, and validates the result.

This is complementary to
[KaroSpaceBuilder](https://github.com/christoffermattssonlangseth/KaroSpaceBuilder),
the desktop GUI for the same export: the Builder puts every knob in front of you
as a clickable form, while the Agent decides the settings for you from the data
and a plain-English intent. Two ways in for two kinds of user — and a natural fit
to combine, with the Agent pre-filling a Builder-style form you can then review
and tweak before exporting.

The app is built on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk)
(Python) and is hostable as a service. Its architecture is **local hands, Claude
brain, sanitized schema only**.

> Also drivable inside Claude Code, without installing anything — see
> [Using it inside Claude Code](#using-it-inside-claude-code) below.

## Data safety (non-negotiable)

Spatial datasets range from non-sensitive (e.g. mouse tissue) to **sensitive
human data** under GDPR governance. **When you are working with sensitive data**,
none of it may reach an LLM server — and rather than leave that to case-by-case
judgement, the agent is built so the guarantee holds by default for *every*
dataset, enforced mechanically rather than trusted to the model's goodwill.
Non-sensitive data loses nothing by the same discipline.

**What the reasoning step is allowed to see — the schema, and only the schema:**
obs column *names*, dtypes, cardinalities (distinct-value counts),
missing/aggregate counts, CLI `--help`, and error text.

**What must never cross the boundary — any data value:** the expression matrix,
cell coordinates, patient identifiers, sample IDs, and the per-column *example
values* that `karospace --inspect-input` prints. The agent reasons about your
experiment from column names, types, and cardinalities alone.

**How the boundary is enforced:** the app runs the model with all built-in tools
disabled, so its only capabilities are seven sanitizing wrappers over the CLIs —
it cannot read a raw file off disk, and `inspect_input` runs
`strip_inspect_examples` before returning anything. (The Claude Code surface,
which has no such code layer, achieves the same by piping inspect through
`sed 's/ examples:.*//'` so example values are stripped in-shell before any
output enters the model's context.)

## Where things run

The heavy compute runs **locally, where the data lives**; only the model is
remote. Nothing but sanitized schema crosses between them.

```
YOUR MACHINE                          ANTHROPIC SERVERS
────────────                          ─────────────────
raw .h5ad ──► karospace (local)
                   │
                   ▼
            sanitize (schema only) ──── HTTPS ──►  Claude (the model)
                   ▲                                   │
                   └──────── flag choices ◄── HTTPS ───┘
                   │
                   ▼
            karospace export (local) ──► viewer.html
```

Because Claude is hosted, *something* leaves your machine on every run — the API
request. The whole safety design exists precisely to guarantee that request
carries only schema (`karospace`, `karospace-companion`, scanpy, DESeq2 all run
locally).

## Install & run

```bash
cd app && pip install -e .             # pulls claude-agent-sdk
export ANTHROPIC_API_KEY=sk-ant-...    # the model runs on Anthropic's servers
karospace-agent build ~/data/my_xenium.h5ad "grid by sample, colour by cell_type"
```

The second argument (the plain-English intent) is optional; without it the agent
picks sensible defaults from the schema. See
[`app/README.md`](app/README.md) for layout, config, and tests.

## Using it inside Claude Code

The same playbook also ships as Claude Code config, so you can drive the whole
loop from a chat with zero infrastructure:

- **Skill** `build-karospace-viewer` — the parameter-selection playbook.
- **Subagent** `karospace-viewer-builder` — runs the loop end-to-end.

```
/build-karospace-viewer  ~/data/my_xenium.h5ad "grid by sample, colour by cell_type, focus on Cd4/Cd8a/Gfap"
```

or delegate the whole thing to the subagent.
