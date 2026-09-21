---
name: build-karospace-viewer
description: Build a KaroSpace HTML viewer from a raw .h5ad / SpatialData .zarr — or from a GEO accession. Use when the user wants to create, generate, or export a KaroSpace spatial-transcriptomics viewer, hands you a spatial dataset, or gives a GEO accession to visualize. Can acquire data from GEO, cluster an un-annotated matrix (leiden) or hand off heavier prep (CellCharter) as a notebook, inspects the data, chooses correct export flags, optionally runs the companion pre-processor, runs the export, and validates the result.
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

### 0a. Acquire from GEO (when the input is an accession, not a file)

If the user gives a GEO accession (`GSExxxxx` / `GSMxxxxx`) or a GEO URL instead
of a path, turn it into a local `.h5ad` first. List what the series holds — public
catalogue metadata only, no data values:

```bash
python scripts/geo_fetch.py manifest GSE243168
```

It prints each sample's title, organism, instrument, inferred platform
(xenium / visium / visium_hd / chromium / …), and every supplementary FILENAME +
size. Pick the sample(s) and platform — many series are multi-platform or
multi-section, so **don't assume**; confirm with the user which platform / GSM(s)
they want. Then build, pulling only the matrix members (for Xenium, out of the
multi-GB `outs.zip` via range requests — never the transcripts table or images):

```bash
python scripts/geo_fetch.py build GSE243168 \
  --platform xenium --gsm GSM7782698 -o /path/GSE243168_xenium.h5ad
```

Supported platforms: `xenium`, `visium`, `merscope`. An unsupported platform or an
unfamiliar layout fails **loudly**, naming what it found — relay that rather than
retrying blindly. The download and assembly run locally; only catalogue metadata
crosses. A fresh build has raw-counts X, no spatial graph, and **no obs
annotations** — so §1b (cluster it) and §5 (companion) both apply. Then continue
from §0.

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

If it genuinely is one section and there are no siblings to merge, build it as a
single section rather than forcing a placeholder: pass `--section-key ""` (an empty
value) and karospace exports the whole dataset as one section. Never repurpose a
cardinality-1 column (e.g. `orig.ident`) as the section key.

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

Then probe the STRUCTURE that `--inspect-input` is blind to (layers, `obsm`,
`obsp`):

```bash
python scripts/inspect_structure.py <input.h5ad>   # add --table <name> for .zarr
```

It prints schema and aggregates only — the X matrix's dtype + storage format and
an `all_integer` flag, layer names+dtypes, `obsm` keys+column counts, `obsp` keys,
and a `spatial_graph_present` flag — **no cell values**, so it needs no `sed`
strip. Two later decisions depend on it: whether a spatial neighbor graph already
exists (§5) and how X is normalized (§3).

### 1b. Prepare an un-annotated matrix — cluster it first

If the inspect output shows **no analysis-derived annotation at all** — no
`cell_type` / `celltype` / `annotation` and no clustering (`leiden`/`louvain`/…) —
the file carries only raw counts and coordinates (a fresh GEO build is the usual
case). A viewer from it could be coloured gene-by-gene only, with nothing for
`--main-cell-annotation`. Create a clustering first — the standard scanpy path
(normalize → log1p → HVG → PCA → neighbors → leiden), run locally:

```bash
python scripts/preprocess.py /path/GSE243168_xenium.h5ad \
  -o /path/GSE243168_xenium_leiden.h5ad --resolution 1.0
```

It writes `obs['leiden']`, a raw `layers['counts']`, and a log1p
`layers['normalized']` (colour from that in §3), and prints an aggregate log only
(cluster count + sizes). Use `leiden` as `--main-cell-annotation` in §2.
Resolution is a **scientific choice**, not a fact: this is a starting point — re-run
at a different `--resolution` if the user wants finer/coarser structure.

For deeper spatial-domain detection (**CellCharter**) or when the researcher wants
to own the clustering, generate a notebook instead — it carries the heavier deps
(scvi-tools + torch) and the biological choices, and runs on their machine/GPU:

```bash
python scripts/gen_notebook.py /path/GSE243168_xenium.h5ad \
  -o /path/prep_GSE243168.ipynb --section-key sample_id
```

That is a **handoff**: tell the user to run the notebook and come back with the
annotated `.h5ad`, then resume from §0. Skip §1b entirely when the file already
carries annotations (most researcher-supplied files do).

### 2. Choose the core flags from the metadata

| Flag | How to pick it |
| --- | --- |
| `--section-key` | Column identifying each section/sample. Look for `sample_id`, `Sample Id`, `sample`, `section`, `slide`, `fov`, `library`, `condition`. Must be categorical, cardinality ~2–100. **A candidate with cardinality 1 is a placeholder** (e.g. `orig.ident`, the Seurat default when never set) — that is *not* a real section key. For a genuinely single-section dataset (no real section column, no siblings to merge — see §0), pass `--section-key ""` (empty) so karospace exports the whole dataset as one section; prefer that over forcing a placeholder. |
| `--main-cell-annotation` | Primary cell-type column. Prefer a human-readable `cell_type`/`celltype`/`annotation` over clustering when both exist. If only clustering exists, use a mid-resolution one (the plain `leiden` if present) as primary — the rest stay exposed via `--cell-annotations`. |
| `--section-metadata` | Categorical experimental variables to show as filter chips: `condition`, `stage`, `timepoint`, `region`, `sex`, `genotype`, `treatment`, `model`, `batch`. Pick the ones that vary across sections. |
| `--cell-annotations` | **Expose EVERY analysis-derived cell annotation, not a curated subset** — users switch between them, so a missed one is a missed view. **Principle (apply it, don't just match names):** a cell annotation is any obs column assigning each cell to a discrete group produced by analysis — clustering, cell-typing, or spatial-domain/niche detection — at any resolution or k. The families are *illustrative, not a whitelist*; catch methods not listed too: clustering (`leiden`, `louvain`, `kmeans`, `walktrap`, `phenograph`, `SNN`, `mclust`, and `<method>_<resolution>` families like `leiden_0_2`…`leiden_4_0`); cell-typing (`cell_type`, `annotation`, `subtype`, `predicted.*`, SingleR/Azimuth-style labels); spatial domains/niches (`CellCharter`, `niche`, `domain`, `UTAG`, `Banksy`, and `<method>_<k>` families like `CellCharter_6`…`CellCharter_30`). The **structural test** is the real net: include any categorical (or low-cardinality integer) obs column, cardinality ~2–300, that isn't an experimental variable (→ `--section-metadata`), an ID (cardinality ≈ cell count, e.g. `cell_id`), or a QC metric. **When unsure, include it.** Never expose ID columns or per-cell continuous QC numerics. **Exception:** columns prefixed `karospace_` (e.g. `karospace_polygon_labels`, `karospace_polygon_count`) and prior-session region/polygon indices (e.g. `polygon_index`) are KaroSpace's *own* round-tripped output from an earlier session, not independent annotations — do **not** sweep them in; mention them so the user can opt in, but leave them out by default. |
| spatial coords | The structure probe shows `obsm`: if `obsm['spatial']` exists, nothing to do. Otherwise pass `--spatial-x`/`--spatial-y` (common names: `x_centroid`/`y_centroid`, `x`/`y`, `center_x`/`center_y`). |
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

**Display normalization — choose from the structure, don't accept the default blind.**
The viewer's coloring values are re-derived by karospace at export, and its
default is wrong for two common inputs. The logic: (1) if
`--statistics-normalized-layer` is set and that layer exists, it's used
**verbatim**; else (2) it takes the counts layer (`--statistics-counts-layer`,
default `counts`), silently falling back to X when that layer is absent, and (3)
applies `--statistics-normalization`: default `RC` (relative counts, scale 10000,
**no log**) or `LogNormalize` (RC then log1p). Both re-normalize whatever step 2
gave them, so:
- *Failure A — double normalization:* X is already normalized (`all_integer=no`)
  with no counts layer → step 2 falls back to X and step 3 re-normalizes it.
- *Failure B — washed out:* X is raw counts (`all_integer=yes`) and default `RC`
  applies no log → a few high-count genes saturate the scale.

Pick from the structure probe (verify flags with `--help`):
- **Preferred (companion route, the §5 default):** the companion writes a
  `normalized` layer (library-size + log1p) — point the viewer at it verbatim with
  `--statistics-normalized-layer normalized`. Nothing is re-derived, so neither
  failure can occur. Use this whenever you build from an enriched file.
- Direct export, a raw-counts layer present (`all_integer=yes`, named like
  `counts`/`raw`): `--statistics-counts-layer <name> --statistics-normalization
  LogNormalize`.
- Direct export, no counts layer but X `all_integer=yes` (X is raw counts): let
  the counts layer fall back to X and set `--statistics-normalization LogNormalize`
  (fixes Failure B).
- Direct export, X `all_integer=no` with a normalized-looking layer
  (`normalized`/`lognorm`/`logcounts`/`data`): `--statistics-normalized-layer
  <name>` (avoids Failure A).
- Direct export, X `all_integer=no` and no usable layer: there is **no** flag for
  "use X as-is" (both normalizations re-derive), so run the companion to get a
  clean `normalized` layer — or, if the user insists on a direct export, warn that
  the coloring may be doubly-normalized.

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

### 5. Companion pre-processing — DEFAULT: enrich first

For spatial data this is the **default route, not an opt-in.** The companion does
several things the viewer needs and `karospace` cannot: it builds the spatial
neighbor graph (`karospace` only *consumes* one from `obsp`; with no
`obsp['spatial_connectivities']` the viewer silently loses every neighbor /
enrichment / interaction / spatially-variable-feature tool), writes a library-size
`normalized` layer (§3), aggregates, and precomputes viewer analytics. Run it
**before** the export:

```
../KaroSpaceCompanion/target/release/karospace-companion prepare <input> \
  --output <enriched.h5ad> --delaunay --groupby <section-key>
```

(same column as `--section-key`), then feed the enriched `.h5ad` to `karospace`.
For large data prefer the fast paths the companion README documents:
`--viewer-cluster-de-method t-test`, `--skip-viewer-interaction-markers`,
`--viewer-analytics-columns <cols>` (the categorical columns you will display);
neighbor-permutation z-scores auto-disable at ≥200k cells.

**An existing graph is not a reason to skip the companion.** If the structure
probe shows `spatial_graph_present=yes`, the graph is only one of the companion's
outputs — skipping would drop the `normalized` layer and the analytics. Run it
anyway. The companion **refuses** to overwrite existing derived outputs and bails
(*"refusing to replace existing '…' without --overwrite-derived"*), so when the
probe shows a graph, a `normalized` layer, or `X_karo_*` in `obsm` already
present, add `--overwrite-derived` to let the pass complete. It re-runs the
Delaunay graph too, but that's cheap in Rust — the point is not to **miss** the
rest. (There's no flag to compute only the analytics and keep the existing graph.)

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

### 8. Verify before you finish — schema-only self-review

Before you report success, re-read your **own** choices against the schema and the
export/companion logs. This review crosses no new information — only what you
already have — so it is always safe. A failed check is a reason to iterate, not a
footnote:

- **Section key is real, not a placeholder** — cardinality ~2–100, never a
  cardinality-1 column (the single-section trap, §0).
- **No annotation left behind** — every analysis-derived cell annotation in `obs`
  made it into `--cell-annotations` (§2), and no ID / per-cell QC numeric /
  `karospace_*` column was swept in by mistake.
- **Coloring is neither double- nor under-normalized** — the `--statistics-*`
  choice matches the structure probe (§3): no already-normalized X fed to
  RC/LogNormalize (Failure A), no raw counts left on the no-log default (Failure B).
- **The neighbor graph exists, or you said why not** — the companion ran (§5), or
  the report names the specific fallback (binary missing / no coordinates found).
- **Pseudobulk matches the design** — `--pseudobulk auto` unless the schema clearly
  shows one sample per group (§3).
- **Both artifacts are actually present** — `validate_output`/stat confirmed the
  viewer AND the `.karospace` (§4, §7), not merely that the command exited 0.
- **The boundary held** — you neither requested nor emitted a data value.

For a stronger check, delegate this pass to the `karospace-viewer-reviewer`
subagent, which sees only the schema, the chosen flags, and the logs and reports
what it would change.

## Before you finish

Summarize: the flags you chose and the reasoning, whether the companion was run,
the output artifact(s), and the open instructions. Surface any warnings the export
printed (e.g. missing neighbor graph, downsampling applied).
