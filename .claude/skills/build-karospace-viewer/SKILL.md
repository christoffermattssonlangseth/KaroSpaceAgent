---
name: build-karospace-viewer
description: Build a KaroSpace HTML viewer from a raw .h5ad / SpatialData .zarr. Use when the user wants to create, generate, or export a KaroSpace spatial-transcriptomics viewer, or hands you a spatial dataset and asks to visualize it. Inspects the data, chooses correct export flags, optionally runs the companion pre-processor, runs the export, and validates the result.
---

# Build a KaroSpace viewer

You are driving the `karospace` CLI (and optionally `karospace-companion`) to turn
a raw dataset into a standalone HTML viewer. The value you add is **choosing the
right flags for this specific dataset** — the export has ~30 knobs and the wrong
choices produce a broken or useless viewer.

## Data-handling rule

Only ever look at **sanitized metadata** — column names, dtypes, example values,
cardinalities, CLI `--help`, error text. Never read or transmit the raw
expression matrix, coordinates, or patient identifiers. All compute runs locally.

## Workflow

### 1. Inspect first — always

```bash
karospace <input.h5ad> --inspect-input
```

For SpatialData `.zarr` with multiple tables, add `--spatialdata-table <name>`.
This lists obs columns, types, example values, and missing-value counts **without**
running the pipeline. Read it before choosing anything. Also skim
`karospace --help` if you're unsure a flag exists — do not assume.

### 2. Choose the core flags from the metadata

| Flag | How to pick it |
| --- | --- |
| `--section-key` | Column identifying each section/sample. Look for `sample_id`, `sample`, `section`, `slide`, `fov`, `library`. Must be categorical, cardinality ~2–100. One value ⇒ single section. |
| `--main-cell-annotation` | Primary cell-type column. Prefer a human-readable `cell_type`/`celltype`/`annotation` over `leiden`/`clusters` when both exist. |
| `--section-metadata` | Categorical experimental variables to show as filter chips: `condition`, `stage`, `timepoint`, `region`, `sex`, `genotype`, `treatment`, `model`, `batch`. Pick the ones that vary across sections. |
| `--cell-annotations` | Extra per-cell annotation columns worth having in dropdowns (`leiden`, `niche`, subtype columns). |
| spatial coords | If `obsm['spatial']` exists, nothing to do. Otherwise pass `--spatial-x`/`--spatial-y` (common names: `x_centroid`/`y_centroid`, `x`/`y`, `center_x`/`center_y`). |
| `--features` | Genes/features to preload. Use whatever biology the user named; otherwise leave to marker auto-embedding. |
| `--modalities` | If the data has protein + RNA (CosMx/multimodal), pass `rna,protein`; else omit. |

### 3. Statistics — match the experimental design

- Default Wilcoxon markers run automatically for `--main-cell-annotation` plus any
  `--statistics-additional-annotations`. Add a second annotation (e.g. `niche`,
  `region`) when it's biologically meaningful.
- **Pseudobulk (`--pseudobulk auto`) only when there are ≥2 biological replicates
  per group.** Check replicate structure from the metadata (e.g. multiple
  `sample_id` per `condition`). One replicate per group ⇒ leave pseudobulk off; it
  would be statistically meaningless.
- `--pathway auto` for RNA-like modalities; set `--pathway-organism` (`Mouse` /
  `Human`) to match the sample.

### 4. Size and storage

- **Large datasets** (many cells/section, e.g. >30–50k): set `--downsample 30000`
  to keep the viewer responsive, and consider lowering `--min-panel-size`.
- **Many features / large payload**: use `--feature-storage sidecar` (writes
  `viewer.html` + `viewer.features.json` + `viewer.features/`). Small payloads:
  `embedded` (default) is fine and gives a single shareable file.
- For one-file sharing of a sidecar viewer, export to `.karospace`.

### 5. Companion pre-processing — when needed

Run `../KaroSpaceCompanion/target/release/karospace-companion prepare ...` first
when the `.h5ad`:
- lacks a spatial neighbor graph (no `obsp['spatial_connectivities']`) and you want
  neighbor/interaction tools → add `--delaunay --groupby <section-key>`;
- needs a normalized layer or precomputed analytics baked in (`--persist-analytics-in-h5ad`).

For large data prefer the fast paths the companion README documents:
`--viewer-cluster-de-method t-test`, `--skip-viewer-interaction-markers`,
`--viewer-analytics-columns <cols>`. Then feed the enriched `.h5ad` to `karospace`.

### 6. Run, read errors, iterate

Build the command, run it, and **read the output**. On failure, the error text
plus the inspect metadata usually tell you the fix (wrong section key, missing
coordinates, a modality that doesn't exist). Adjust and re-run. Don't guess flag
values into existence — verify against `--help` and the inspect output.

### 7. Validate the result

- Embedded: `viewer.html` exists and is non-trivially sized.
- Sidecar: `viewer.html` **and** `viewer.features.json` **and** `viewer.features/`
  all present with matching relative paths. Remind the user sidecar viewers must be
  served over HTTP (`python -m http.server`), not opened via `file://`.
- Report exactly what was produced, the key flags chosen and *why*, and how to open it.

## Before you finish

Summarize: the flags you chose and the reasoning, whether the companion was run,
the output artifact(s), and the open instructions. Surface any warnings the export
printed (e.g. missing neighbor graph, downsampling applied).
