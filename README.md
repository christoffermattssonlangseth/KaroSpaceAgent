# KaroSpaceAgent

A standalone agent that builds a [KaroSpace](../KaroSpace) spatial-transcriptomics
viewer from a raw `.h5ad` / SpatialData `.zarr` file.

```bash
karospace-agent build ~/data/my_xenium.h5ad "grid by sample, colour by cell_type"
```

It can also **start from a public GEO accession** instead of a local file:
`geo_manifest` lists a series' samples, files, and inferred platform, and
`geo_build` downloads only the matrix members needed to construct the AnnData
— for a Xenium sample it range-fetches `cell_feature_matrix.h5` + `cells.parquet`
out of the multi-GB `outs.zip` rather than the whole archive — and writes a
minimal `.h5ad` (raw counts + spatial coordinates) ready for the pipeline above.
Xenium, Visium, and MERSCOPE/MERFISH (Vizgen) layouts are supported; other
platforms report what an assembler would need. Only public GEO catalogue
metadata crosses the boundary; the download and assembly run locally.

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
disabled, so its only capabilities are a small set of sanitizing wrappers over the
CLIs — it cannot read a raw file off disk, and `inspect_input` runs
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
karospace-agent auth                   # which Claude credential will be used, and how to sign in
karospace-agent build ~/data/my_xenium.h5ad "grid by sample, colour by cell_type"
```

## Authentication

The model runs on Anthropic's servers, so a credential is needed. Two routes are
supported, and `karospace-agent auth` tells you which one is active:

- **Sign in with a Console account (recommended, no key to paste).** Run
  `claude`, pick *Anthropic Console account*, then *Sign in with your Console
  account*, and finish in the browser. That stores an auto-refreshing login
  that this app picks up automatically. Usage is billed to your organisation's
  API credits under Anthropic's commercial terms, which is also where an
  organisation manages data-retention settings — the right footing for human
  data.
- **An API key.** `export ANTHROPIC_API_KEY=sk-ant-...` from
  [platform.claude.com](https://platform.claude.com). Workload Identity
  Federation and cloud providers (Bedrock, Vertex, Foundry) work too.

**A claude.ai subscription login (Pro/Max/Team/Enterprise, or a
`claude setup-token`) is not permitted.** Anthropic's Agent SDK terms reserve
claude.ai logins for Claude Code and claude.ai themselves; third-party agents
must use the routes above. The preflight warns if it finds one and refuses to
call it a working setup.

The second argument (the plain-English intent) is optional; without it the agent
picks sensible defaults from the schema. See
[`app/README.md`](app/README.md) for layout, config, and tests.

### Chat mode

`build` is one shot. `chat` keeps the conversation open, so the agent can ask
you a question (the single-section trap, whether to downsample) and act on your
answer, and you can iterate on a built viewer without starting over:

```bash
karospace-agent chat ~/data/my_xenium.h5ad "grid by sample"   # opens with a build
karospace-agent chat                                          # or just start talking
```

```
you> add Cd4 and Cd8a to the preloaded features and rebuild
you> /quit
```

Ctrl-C while the agent is working interrupts that turn and returns you to the
prompt (twice quits); at the prompt it exits.

### Browser mode

The same conversation in a browser tab, for people who would rather not live
in a terminal:

```bash
pip install -e '.[web]'                     # adds starlette + uvicorn
karospace-agent web ~/data/my_xenium.h5ad "grid by sample"
# -> http://127.0.0.1:8765/
```

It serves one conversation on localhost: model replies, tool calls, and the
live export log (collapsible), with a Stop button that interrupts the running
turn. Refreshing the tab replays the transcript. The server must run on the
machine that holds the data — the tools spawn `karospace` locally — and it has
no auth, so keep it on `127.0.0.1` (the default; `--host` warns otherwise).

Same tools, same prompt, same boundary — only the schema reaches the model
through the tools. The one new channel is you: what you type is sent to
Anthropic as-is, so give file paths and column *names*, never sample IDs,
coordinates, or other values. The agent is told never to ask for them.

## Using it inside Claude Code

The same playbook also ships as Claude Code config, so you can drive the whole
loop from a chat with zero infrastructure:

- **Skill** `build-karospace-viewer` — the parameter-selection playbook.
- **Subagent** `karospace-viewer-builder` — runs the loop end-to-end.

```
/build-karospace-viewer  ~/data/my_xenium.h5ad "grid by sample, colour by cell_type, focus on Cd4/Cd8a/Gfap"
```

or delegate the whole thing to the subagent.
