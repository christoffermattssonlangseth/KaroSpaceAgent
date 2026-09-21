# Agent playbook: stages, verification, tool scoping

This note records why the agent is structured the way it is, so the shape is a
decision on the record rather than an accident of how the prompt grew.

## The problem

The build knowledge started as one ~250-line system prompt (`prompt.py`) plus two
near-duplicate Claude Code files (`SKILL.md`, the `karospace-viewer-builder`
subagent). A monolith is hard to edit without fear (every change touches the whole
string), hard to test (nothing pins its shape), and hard to keep in sync across
the three surfaces. As the tool count and the number of failure modes grew, we
wanted three things: modularity, a built-in check against our recurring mistakes,
and a tidier way to present a growing toolset.

## What we did

### 1. The playbook is a registry of pipeline-stage skills

`playbook.py` holds the decision content as an ordered list of named stages that
follow **our** build pipeline:

    acquire → inspect → design → size → enrich → run → deliver → verify

`build_system_prompt()` composes them (framed by a boundary PREAMBLE, a
stage-grouped TOOLBOX, and a FINISH block) into `SYSTEM_PROMPT`. Each stage is a
self-contained, individually testable unit; adding one is appending to
`WORKFLOW_STAGES`, not surgery on a string. `prompt.py` is now a thin assembly
module that re-exports `SYSTEM_PROMPT` / `CHAT_ADDENDUM`, so nothing downstream
changed.

The stages map onto the two Claude Code surfaces, which stay the human-authored
mirror of the same pipeline.

### 2. A schema-only verify stage (and a reviewer subagent)

The last workflow stage, **verify**, is a self-review the agent runs before
reporting success. It re-reads the flag choices it already made against the schema
and the run logs and walks a checklist of the failure modes this codebase keeps
warning about: a placeholder (cardinality-1) section key, analysis annotations
left out of `--cell-annotations`, display normalization that double- or
under-normalizes the coloring (Failures A/B), a companion silently skipped, wrong
pseudobulk, artifacts assumed from exit code 0 rather than stat-confirmed, and any
breach of the data boundary.

The key property: **this review crosses no new information.** It reasons over what
the build already produced, so it adds a quality gate without widening what leaves
the machine — consistent with the "only schema crosses" rule. It is prompt/config
only; there is no second data path. On the Claude Code surface the same check is
also available as an independent `karospace-viewer-reviewer` subagent that sees
only schema + flags + logs.

### 3. Tools grouped by stage, not embedding-ranked

The toolbox is presented grouped by the stage that uses each tool
(acquire/inspect/enrich/export/deliver) instead of as a flat list. At ~10 tools
the whole set fits in view, so grouping for orientation is the right-sized design;
embedding-based tool *retrieval* would be machinery for a problem we do not have.
If the toolset ever grows past ~20, revisit — that is the point where retrieval
starts to earn its keep.

## Prior art and how ours differs

The stage-decomposed skills, the verification pass, and tool scoping are common
agent-architecture patterns; the immediate prompt to adopt them here was
[Genentech/SpatialAgent](https://github.com/Genentech/SpatialAgent) (MIT), a
spatial-omics agent built on a Plan–Act–Conclude loop with a large skill library,
a verification subagent, and embedding-based tool retrieval over ~70 tools.

We borrowed the *ideas*, not the design, because our core constraint is the
opposite of theirs:

- **No code-execution core.** SpatialAgent's engine is `execute_python` /
  `execute_bash` — the model runs arbitrary code against the data. That is exactly
  what our boundary forbids. Our capabilities are a fixed set of sanitizing
  wrappers; the model never touches raw data. So we adopted the *organizing*
  patterns and deliberately left the engine behind.
- **Our stages are the KaroSpace pipeline** (acquire→…→deliver→verify), and our
  verify checklist is *our* concrete failure modes (the single-section trap,
  normalization Failures A/B, the companion-skip), not a generic critique.
- **Right-sized tooling.** Ten sanitizing tools do not need embedding retrieval;
  grouping suffices.

Recorded here so the lineage is explicit and the divergence is intentional.
