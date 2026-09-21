# Dataset prep: in-agent clustering vs. generated notebook

## The gap

Acquisition (`geo_build`) yields raw counts + coordinates and **no obs
annotations**. Neither `karospace` nor the companion does de-novo transcriptomic
clustering — both *consume* an annotation column that must already exist. So a
freshly acquired dataset has nothing for `--main-cell-annotation`, and a viewer
built from it can only be coloured gene-by-gene. Prep has to *create* that column.

## Two paths, split by weight and by who makes the scientific call

**`run_preprocess` — in-agent, light.** scanpy `normalize → log1p → HVG → PCA →
neighbors → leiden`, run locally. Writes `layers['counts']` (raw, preserved),
`layers['normalized']` (log1p), and `obs['leiden']`. Deps (scanpy, leidenalg) are
already in the karospace env, so the agent can run it headless and continue the
build. Only aggregate counts (cluster count, per-cluster sizes) cross the
boundary — the same class of fact as the cardinalities `inspect_input` already
emits. This is the "just make me a viewer" path.

**`generate_notebook` — handoff, heavy.** Emits a parameterized notebook
(`normalize → leiden → CellCharter → annotated .h5ad`) the researcher runs on
their own machine. This is where **CellCharter** spatial-domain detection lives,
for two reasons:

1. **Weight.** CellCharter pulls in `scvi-tools` + `torch` (+ `squidpy`) and wants
   a GPU. Those are not — and should not be — in the agent's runtime env.
2. **Scientific ownership.** CellCharter's domain count is a stability-selected
   biological choice; leiden resolution likewise. Having a headless agent silently
   pick these is exactly what a careful analysis should avoid. A notebook puts the
   choice, and the inspection, back with the researcher.

The generator only templates from schema-level parameters (paths, the section-key
column *name*, gene names, numbers) — it reads no data, so nothing crosses the
boundary. The notebook runs entirely on the researcher's machine; the only thing
that later returns to the agent is the *schema* of the annotated file it writes.

## Why not run CellCharter in-agent too

We deliberately did not add a heavyweight in-agent clustering tool. It would drag
GPU/torch deps into the service runtime, and it would move a real biological
decision behind the boundary where the researcher can't see it. The notebook is
the honest home for that work. `run_preprocess` stays intentionally light: one
sensible, reported, re-runnable default (leiden @ resolution 1.0), nothing that
claims to be the final analysis.

## R `.rds` inputs: convert, don't re-cluster

A related gap is the *input format*. Much published spatial/single-cell analysis
lives in R objects — Seurat / SingleCellExperiment / SpatialExperiment `.rds`
files — which KaroSpace can't ingest. And crucially, when a GEO sample ships both
a raw matrix and an analyzed `.rds` (e.g. a Xenium `*_final_*_object.rds`), the
`.rds` is usually where the authors' *curated cell types, embeddings, and spatial
coordinates* live. `geo_build` only pulls the raw matrix, so without a converter
the agent would throw that analysis away and re-cluster from scratch with leiden.

**`rds_inspect` / `rds_convert`** wrap the
[`rds2h5ad`](https://github.com/christoffermattssonlangseth/RDStoH5AD) CLI (a
Python front end over an R backend: R owns the S4/Seurat deserialization,
`zellkonverter` writes the `.h5ad`, sparse stays sparse). Boundary: `rds2h5ad
inspect` emits schema only — object type, assay/layer/reduced-dim *names*,
cell/gene counts, `has_spatial` — no values, so it needs no example stripping; the
conversion runs locally and the model sees only the written file's schema. It's an
*optional* dependency (needs R + `zellkonverter`); when absent the tool says so and
the agent falls back to the raw-matrix path.

### Why "ask each time" when both a matrix and an `.rds` exist

The two paths trade off and neither is universally right:

- **raw matrix + `run_preprocess`** — light, no R dependency, but a de-novo leiden
  that discards the authors' annotations;
- **the analyzed `.rds`** — the authors' real cell types + UMAP + coordinates, but
  a larger download and an R conversion.

Which one a researcher wants is a per-dataset scientific call ("show me *their*
bladder cell types" vs. "just get me a quick viewer"), not something the agent
should decide silently. So the policy is to **surface both and let the user pick**;
only when running one-shot with no one to ask does it default to the lighter
raw-matrix path and say so. A converted `.rds` usually already carries
annotations, so the clustering prep (§1b) is skipped, but the companion (§5) and
normalization (§3) steps still apply.
