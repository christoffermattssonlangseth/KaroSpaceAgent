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

Datasets range from non-sensitive (e.g. mouse) to sensitive human data under
GDPR governance; since you can't reliably tell which mid-task, treat
the boundary as always-on. Per the data management plan, only the **schema** may
enter your context — column names, dtypes, cardinalities (distinct-value counts),
missing/aggregate counts, CLI `--help`, error text. **No data values** may cross:
not the expression
matrix, coordinates, patient identifiers, sample IDs, or the per-column *example
values* that `--inspect-input` prints. That is why the inspect command in step 1
is piped through a strip step — always run it that way, never the raw form.
Reason from names, types, and cardinalities alone. All compute runs locally.

## Workflow

### 0. Is this the right file? (single-section trap)

Datasets often arrive as **per-section files** (`..._P1_L_...`, `..._P1_NL_...`) that
have been stripped of sample-level metadata — the tell-tale sign is that the only
section-key candidate (e.g. `orig.ident`) has **cardinality 1** (a single distinct
value — a placeholder) and there are **no** `sample_id` / `condition` /
`sample_batch` columns. Such a file can only ever make a
single-section viewer with no cross-condition statistics and no replicates.

If the inspect output shows this, **stop and flag it to the user** before building.
The useful viewer is built from the **merged** file that carries `sample_id`,
`condition`, and `sample_batch` in obs (paired designs, e.g. Lesional vs
Non-lesional, with the patient/subject as the pseudobulk replicate unit).

If the user hands you the sibling per-section files, merge them with the repo
helper (adds `sample_id` / `condition` / `sample_batch`, runs locally on disk):

```bash
python scripts/merge_sections.py \
  --section P1_L:/path/Xenium_P1_L_annotated_vF.h5ad:Lesional \
  --section P1_NL:/path/Xenium_P1_NL_annotated_vF.h5ad:Non-lesional \
  --output /path/Xenium_P1_LNL_merged.h5ad
```

Then build from the merged file. Note a single patient's L+NL is still 1 replicate
per condition — enough for a side-by-side viewer, **not** for condition pseudobulk
(that needs ≥2 patients, i.e. more `--section` entries).

### 1. Inspect first — always

```bash
karospace <input.h5ad> --inspect-input | sed 's/ examples:.*//'
```

The `sed` step strips the per-column example VALUES (coordinates, sample IDs,
category labels) so only the schema enters your context — run inspect **only**
this way, never the bare `karospace <input.h5ad> --inspect-input`. For
SpatialData `.zarr` with multiple tables, add `--spatialdata-table <name>` before
the pipe. What remains is obs column names, types, cardinalities, and
missing-value counts, **without** running the pipeline. Read it before choosing
anything. Also skim `karospace --help` if you're unsure a flag exists — do not
assume.

### 2. Choose the core flags from the metadata

| Flag | How to pick it |
| --- | --- |
| `--section-key` | Column identifying each section/sample. Look for `sample_id`, `Sample Id`, `sample`, `section`, `slide`, `fov`, `library`, `condition`. Must be categorical, cardinality ~2–100. **A candidate with cardinality 1 is a placeholder** (e.g. `orig.ident`, the Seurat default when never set) — that is *not* a real section key. |
| `--main-cell-annotation` | Primary cell-type column. Prefer a human-readable `cell_type`/`celltype`/`annotation` over clustering when both exist. If only clustering exists, use a mid-resolution one (the plain `leiden` if present) as primary — the rest stay exposed via `--cell-annotations`. |
| `--section-metadata` | Categorical experimental variables to show as filter chips: `condition`, `stage`, `timepoint`, `region`, `sex`, `genotype`, `treatment`, `model`, `batch`. Pick the ones that vary across sections. |
| `--cell-annotations` | **Expose EVERY analysis-derived cell annotation, not a curated subset** — users switch between them, so a missed one is a missed view. **Principle (apply it, don't just match names):** a cell annotation is any obs column assigning each cell to a discrete group produced by analysis — clustering, cell-typing, or spatial-domain/niche detection — at any resolution or k. The families are *illustrative, not a whitelist*; catch methods not listed too: clustering (`leiden`, `louvain`, `kmeans`, `walktrap`, `phenograph`, `SNN`, `mclust`, and `<method>_<resolution>` families like `leiden_0_2`…`leiden_4_0`); cell-typing (`cell_type`, `annotation`, `subtype`, `predicted.*`, SingleR/Azimuth-style labels); spatial domains/niches (`CellCharter`, `niche`, `domain`, `UTAG`, `Banksy`, and `<method>_<k>` families like `CellCharter_6`…`CellCharter_30`). The **structural test** is the real net: include any categorical (or low-cardinality integer) obs column, cardinality ~2–300, that isn't an experimental variable (→ `--section-metadata`), an ID (cardinality ≈ cell count, e.g. `cell_id`), or a QC metric. **When unsure, include it.** Never expose ID columns or per-cell continuous QC numerics. **Exception:** columns prefixed `karospace_` (e.g. `karospace_polygon_labels`, `karospace_polygon_count`) and prior-session region/polygon indices (e.g. `polygon_index`) are KaroSpace's *own* round-tripped output from an earlier session, not independent annotations — do **not** sweep them in; mention them so the user can opt in, but leave them out by default. |
| spatial coords | If `obsm['spatial']` exists, nothing to do. Otherwise pass `--spatial-x`/`--spatial-y` (common names: `x_centroid`/`y_centroid`, `x`/`y`, `center_x`/`center_y`). |
| `--features` | Genes/features to preload. Use whatever biology the user named; otherwise leave to marker auto-embedding. |
| `--modalities` | If the data has protein + RNA (CosMx/multimodal), pass `rna,protein`; else omit. |

### 3. Statistics — match the experimental design

- Default Wilcoxon markers run automatically for `--main-cell-annotation` plus any
  `--statistics-additional-annotations`. Add a second annotation (e.g. `niche`,
  `region`) when it's biologically meaningful.
- **Pseudobulk (`--pseudobulk auto`) whenever the design *plausibly* has ≥2
  biological replicates per group.** You cannot verify per-group replicate counts
  from the schema (you see cardinalities, not the `condition × replicate`
  cross-tab), so don't try — prefer `auto` and let karospace's **local** guard
  decide per contrast: it enforces `--pseudobulk-min-replicates` (≥2 always
  required) on the real counts and *skips* — does not error on — any contrast
  below threshold. The schema signal for "plausibly replicated": a
  biological-sample column (`--section-key` / `sample_id` / `animal` / `subject` /
  `patient`) whose cardinality **exceeds the number of condition groups** (more
  samples than conditions). Leave pseudobulk off only when the schema clearly
  shows one sample per group (sample cardinality == condition cardinality) or
  there's no replicate/sample column — then it's meaningless. When unsure, pass
  `auto`; the local guard is the real gate.
- `--pathway auto` for RNA-like modalities; set `--pathway-organism` (`Mouse` /
  `Human`) to match the sample.

### 4. Size and storage

- **Do NOT downsample by default.** Export all cells — researchers need the full
  data, and dropping cells silently distorts the spatial picture and any
  statistics. Prefer other levers for size/performance (below). Only consider
  `--downsample` as a last resort for genuine browser-performance problems, and
  only after saying so explicitly and confirming with the user first — never
  silently.
- **Many features / large payload**: use `--feature-storage sidecar` (writes
  `viewer.html` + `viewer.features.json` + `viewer.features/`). This is the right
  size lever — it keeps every cell but moves feature vectors out of the HTML.
  Small payloads: `embedded` (default) is fine and gives a single shareable file.
- **Rendering performance** without dropping cells: lower `--min-panel-size`,
  keep the neighbor-graph overlay off unless needed.

**Default deliverable: produce BOTH.** The team usually wants the sidecar viewer
*and* the single-file `.karospace` package. So after the sidecar export succeeds,
package it (no recompute) with:

```bash
karospace package-sidecar <viewer.html> --output <name>.karospace
```

This adds `<name>.karospace` + `<name>.loader.html` alongside the sidecar files.
Deliver both unless the user says otherwise.

### 5. Companion pre-processing — DEFAULT: build the spatial graph first

For spatial data this is the **default route, not an opt-in.** `karospace` never
builds a spatial neighbor graph itself — it only *consumes* one from `obsp`; with
no `obsp['spatial_connectivities']` the viewer silently loses every neighbor /
enrichment / interaction / spatially-variable-feature tool. `--inspect-input`
reports obs columns only (no `obsp`/`obsm`), so you can't tell whether a graph is
already present — assume it is absent and build it. Run the companion **before**
the export:

```
../KaroSpaceCompanion/target/release/karospace-companion prepare <input> \
  --output <enriched.h5ad> --delaunay --groupby <section-key>
```

(same column as `--section-key`), then feed the enriched `.h5ad` to `karospace`.
For large data prefer the fast paths the companion README documents:
`--viewer-cluster-de-method t-test`, `--skip-viewer-interaction-markers`,
`--viewer-analytics-columns <cols>` (the categorical columns you will display);
neighbor-permutation z-scores auto-disable at ≥200k cells.

**Fall back gracefully — never fail the whole job because the companion couldn't run:**
- Binary missing (not built) → export directly from the original file and tell the
  user the neighbor/interaction tools are absent until they build it
  (`cargo build --release` in `KaroSpaceCompanion`, or set `KAROSPACE_COMPANION`).
- Companion errors *"no spatial coordinates found …"* → its coordinate discovery is
  fixed (`obsm/spatial`, `obsm/X_spatial`, or obs pairs `array_col`/`array_row`,
  `pxl_col_in_fullres`/`pxl_row_in_fullres`, `x`/`y`; it has **no** `--spatial-x/-y`
  flag). Export directly with `karospace`'s own `--spatial-x`/`--spatial-y` (§2) so
  the viewer still gets coordinates, and note the graph was skipped.

Skip the companion only on one of those fallbacks or when the user opts out.

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
- `.karospace` package: `<name>.karospace` **and** `<name>.loader.html` present.
  Open via the hosted loader at karospace.se/open or the local `.loader.html`.
- Report exactly what was produced (both the sidecar viewer and the `.karospace`),
  the key flags chosen and *why*, and how to open each.

## Before you finish

Summarize: the flags you chose and the reasoning, whether the companion was run,
the output artifact(s), and the open instructions. Surface any warnings the export
printed (e.g. missing neighbor graph, downsampling applied).
