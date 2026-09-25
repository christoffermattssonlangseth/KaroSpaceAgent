"""The model's entire capability surface: sanitizing wrappers over the CLIs.

Each `@tool` runs a local subprocess (via `commands.py`) and returns only
sanitized, truncated text (via `sanitize.py`). There is deliberately no tool
that reads file bytes — the model can inspect metadata, run the pipeline, and
stat the output, but it can never pull the expression matrix or coordinates
into context. Built-in Read/Bash tools are disabled in `agent.py`, so this list
is the whole of what Claude can do.
"""

from __future__ import annotations

from typing import Annotated, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import commands
from .sanitize import stat_paths, strip_inspect_examples, truncate

SERVER_NAME = "karospace"


def _result(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


def _report(rr: commands.RunResult, label: str) -> dict[str, Any]:
    """Capture a local report for privacy.Boundary; never send it directly."""
    body = (
        f"$ {label}\n"
        f"exit code: {rr.returncode}"
        + ("  (TIMED OUT)" if rr.timed_out else "")
        + "\n\n--- stdout ---\n"
        + truncate(rr.stdout)
        + "\n\n--- stderr ---\n"
        + truncate(rr.stderr)
    )
    result = _result(body, is_error=not rr.ok)
    # Local-only input to the privacy boundary. Never passed through the SDK:
    # build_server wraps every handler and constructs a fresh outbound result.
    result["_local"] = {"returncode": rr.returncode, "stdout": rr.stdout,
                        "stderr": rr.stderr, "timed_out": rr.timed_out}
    return result


# --- Inspection -----------------------------------------------------------

@tool(
    "inspect_input",
    "Inspect a raw .h5ad / SpatialData .zarr and return the SCHEMA only: obs "
    "column names, type, cardinality (number of distinct values), missing counts, "
    "and feature counts by modality. Runs `karospace <input> --inspect-input` "
    "without building a viewer. Always call this first. Example VALUES are "
    "deliberately withheld (compliance: cell coordinates, sample IDs, and other "
    "values must not leave the local machine) — reason from column names, types, "
    "and cardinalities. The expression matrix and coordinates are never exposed.",
    {
        "input_path": Annotated[str, "Path to the .h5ad file or .zarr store."],
        "spatialdata_table": Annotated[
            str, "For SpatialData .zarr with multiple tables, the table key. Else ''."
        ],
    },
)
async def inspect_input(args: dict[str, Any]) -> dict[str, Any]:
    argv = [args["input_path"], "--inspect-input"]
    table = (args.get("spatialdata_table") or "").strip()
    if table:
        argv += ["--spatialdata-table", table]
    # stream=False: do NOT live-tee this run. Its raw stdout carries the example
    # VALUES the boundary strips — teeing would print them to the local console
    # and scrollback before the strip below removes them from the model's view.
    rr = commands.run_karospace(argv, timeout=600, stream=False)
    # Enforce the boundary: strip example VALUES, keep only the schema.
    rr.stdout = strip_inspect_examples(rr.stdout)
    return _report(rr, f"karospace {' '.join(argv)}")


@tool(
    "inspect_structure",
    "Report the STRUCTURE that inspect_input cannot see: the X matrix's dtype and "
    "storage format and whether its values are all integers (raw counts) or not; "
    "raw.X if present; the names+dtypes of layers; the keys of obsm (with column "
    "counts); and the keys of obsp — including a spatial_graph_present flag "
    "(obsp spatial_connectivities/distances). Runs the local structural probe. "
    "Schema and aggregates only — matrix dtypes/formats, key NAMES, obsm shapes, "
    "and one all_integer boolean per matrix; no cell values, coordinates, or "
    "labels cross. Call it after inspect_input to decide the --statistics-* "
    "normalization flags and whether the companion still needs to build a graph.",
    {
        "input_path": Annotated[str, "Path to the .h5ad file or .zarr store."],
        "spatialdata_table": Annotated[
            str, "For a .zarr with multiple tables, the table key. Else ''."
        ],
    },
)
async def inspect_structure(args: dict[str, Any]) -> dict[str, Any]:
    table = (args.get("spatialdata_table") or "").strip()
    rr = commands.run_structure(args["input_path"], table=table)
    return _report(rr, f"inspect_structure.py {args['input_path']}")


@tool(
    "check_readiness",
    "Check a local .h5ad or AnnData/SpatialData .zarr before expensive processing. "
    "Scans expression and coordinates in bounded chunks; checks section labels, "
    "raw counts when required, output writability, free disk and available memory. "
    "Returns only aggregate counts and fixed diagnostics. Do not proceed if ready "
    "is false; resolve errors first. Resource estimates are guidance, not peak-memory "
    "guarantees. Review warnings. Raw counts are integer-valued nonnegative data; "
    "this check cannot establish their biological provenance.",
    {"input_path": Annotated[str, "Input .h5ad/.zarr."],
     "output_dir": Annotated[str, "Directory planned for outputs."],
     "section_key": Annotated[str, "Section column alias, or empty if not yet selected."],
     "coords_key": Annotated[str, "Spatial embedding alias; default spatial."],
     "counts_layer": Annotated[str, "Raw-count layer alias, or empty to check X."],
     "require_counts": Annotated[bool, "True before raw-count QC or clustering."],
     "table": Annotated[str, "SpatialData table alias, or empty for a single table." ]},
)
async def check_readiness(args: dict[str, Any]) -> dict[str, Any]:
    rr = commands.run_readiness(
        args["input_path"], args["output_dir"], args.get("section_key", ""),
        args.get("coords_key") or "spatial", args.get("counts_layer", ""),
        bool(args.get("require_counts", False)), args.get("table", ""))
    return _report(rr, "local dataset readiness check")


@tool(
    "cli_help",
    "Show `karospace --help` (or a verb's help) so you can verify a flag exists "
    "before using it. Never invent flags — check here.",
    {"verb": Annotated[str, "One of '', 'package-sidecar', 'ome-convert'."]},
)
async def cli_help(args: dict[str, Any]) -> dict[str, Any]:
    verb = (args.get("verb") or "").strip()
    if verb not in ("", "package-sidecar", "ome-convert"):
        return _result("Unsupported help topic.", is_error=True)
    argv = ([verb] if verb else []) + ["--help"]
    rr = commands.run_karospace(argv, timeout=60)
    # argparse prints help to stdout and exits 0; some verbs exit 0 too.
    return _report(rr, "karospace help")


# --- Acquisition (GEO) ----------------------------------------------------

@tool(
    "geo_manifest",
    "List a public GEO accession's samples and supplementary files so you can "
    "choose what to build. Give a series (GSExxxxx) or one sample (GSMxxxxx); "
    "returns each sample's title, organism, instrument, inferred platform "
    "(xenium / visium / visium_hd / chromium / ...), and every supplementary "
    "FILENAME with its size. Only public GEO catalogue metadata crosses — no "
    "data values. Call this before geo_build to pick the --gsm ids and platform. "
    "Note which samples are the platform the user asked for; sizes reveal the "
    "big bundles (e.g. a Xenium *_outs.zip) that geo_build reads selectively.",
    {
        "accession": Annotated[str, "GEO accession: a series 'GSE...' or a sample 'GSM...'."],
        "with_sizes": Annotated[
            bool, "HEAD each file for its size (slower). Pass false to skip."
        ],
    },
)
async def geo_manifest(args: dict[str, Any]) -> dict[str, Any]:
    accession = str(args["accession"]).strip()
    with_sizes = bool(args.get("with_sizes", True))
    rr = commands.run_geo_manifest(accession, sizes=with_sizes)
    return _report(rr, f"geo_fetch.py manifest {accession}")


@tool(
    "geo_build",
    "Download ONLY the matrix members of the chosen GEO samples and assemble a "
    "minimal .h5ad (raw counts in X + spatial coordinates in obsm) ready for the "
    "pipeline. For Xenium it pulls cell_feature_matrix.h5 + cells.parquet out of "
    "each sample's multi-GB *_outs.zip via range requests — the transcripts "
    "table and morphology images are never transferred, and the 31 GB series "
    "RAW.tar is avoided. Choose gsm_ids and platform from geo_manifest first. "
    "Returns an aggregate build log only (members pulled + sizes, then cell/gene "
    "counts and obs/var/obsm key names) — then call inspect_input / "
    "inspect_structure on the written file for the schema. Supported platforms: "
    "'xenium' (pulls matrix members from the outs.zip), 'visium' (filtered "
    "matrix + tissue_positions), and 'merscope' (Vizgen cell_by_gene + "
    "cell_metadata). Others report what an assembler would need.",
    {
        "accession": Annotated[str, "GEO accession the samples belong to (GSE or GSM)."],
        "gsm_ids": Annotated[
            list, "Sample accessions to include. Empty = all samples of `platform`."
        ],
        "platform": Annotated[str, "Assembler to use. 'xenium' is implemented."],
        "output": Annotated[str, "Output .h5ad path."],
        "include_control_features": Annotated[
            bool, "Keep negative-control / blank probes. Default false (Gene Expression only)."
        ],
    },
)
async def geo_build(args: dict[str, Any]) -> dict[str, Any]:
    accession = str(args["accession"]).strip()
    gsm_ids = [str(g).strip() for g in args.get("gsm_ids", []) if str(g).strip()]
    platform = (str(args.get("platform") or "xenium")).strip()
    rr = commands.run_geo_build(
        accession,
        gsm_ids,
        platform,
        str(args["output"]),
        include_control=bool(args.get("include_control_features", False)),
    )
    return _report(rr, f"geo_fetch.py build {accession} --platform {platform}")


@tool(
    "geo_fetch_file",
    "Download ONE supplementary file from a GEO sample straight to local disk — "
    "the files geo_build deliberately skips. Its main use: pull an analyzed "
    "'*_object.rds' (a Xenium/Seurat/SingleCellExperiment object carrying the "
    "authors' curated cell types + embeddings + coordinates) so you can convert "
    "it with rds_inspect/rds_convert, instead of asking the user to download it "
    "or throwing the analysis away and re-clustering the raw matrix. Match the "
    "file by a substring of its FILENAME (from geo_manifest), e.g. '.rds' or "
    "'final_xenium_object'; the pattern must select exactly one file or it errors "
    "with the candidates. Downloads bytes to local disk only — nothing but the "
    "aggregate log crosses the boundary. Returns the local path; then call "
    "rds_inspect on it (for an .rds) or inspect_input / inspect_structure.",
    {
        "accession": Annotated[str, "GEO accession the sample belongs to (GSE or GSM)."],
        "gsm": Annotated[str, "The sample (GSMxxxxx) whose file to download."],
        "match": Annotated[
            str, "Filename substring selecting exactly one file, e.g. '.rds'."
        ],
        "output_dir": Annotated[str, "Local directory to download the file into."],
        "gunzip": Annotated[
            bool, "Decompress a .gz payload after download. Default false (an .rds is not gzipped)."
        ],
    },
)
async def geo_fetch_file(args: dict[str, Any]) -> dict[str, Any]:
    accession = str(args["accession"]).strip()
    gsm = str(args["gsm"]).strip()
    match = str(args["match"]).strip()
    rr = commands.run_geo_fetch_file(
        accession,
        gsm,
        match,
        str(args["output_dir"]),
        gunzip=bool(args.get("gunzip", False)),
    )
    return _report(rr, f"geo_fetch.py fetch {accession} --gsm {gsm} --match {match}")


@tool(
    "rds_inspect",
    "Inspect an R .rds / .RData object (a Seurat, SingleCellExperiment, or "
    "SpatialExperiment) and return the SCHEMA only, via the rds2h5ad R backend. "
    "Emits names and counts: object type, the selected + available assays, layer "
    "names, reduced-dim (embedding) names, cell and gene counts, and a has_spatial "
    "flag — NO data values, so it needs no example stripping. Use this to decide "
    "whether an analyzed .rds is worth converting (e.g. a GEO Xenium "
    "'*_final_*_object.rds' usually carries the authors' curated cell-type "
    "annotations + UMAP + coordinates that the raw matrix lacks) and which --assay "
    "to convert. Requires rds2h5ad on PATH (R + zellkonverter).",
    {
        "input_path": Annotated[str, "Path to the .rds or .RData file."],
        "assay": Annotated[
            str, "Assay to inspect. '' lets the backend choose a default."
        ],
    },
)
async def rds_inspect(args: dict[str, Any]) -> dict[str, Any]:
    assay = (args.get("assay") or "").strip()
    rr = commands.run_rds_inspect(str(args["input_path"]), assay=assay)
    return _report(rr, f"rds2h5ad inspect {args['input_path']}")


@tool(
    "rds_convert",
    "Convert an R .rds / .RData object to a KaroSpace-ingestible .h5ad, via the "
    "rds2h5ad R backend (R owns the Seurat/SingleCellExperiment deserialization; "
    "zellkonverter writes the .h5ad; sparse matrices stay sparse). Use this when "
    "the analyzed object carries annotations/embeddings the raw matrix doesn't — "
    "e.g. prefer a GEO sample's '*_final_*_object.rds' over the bare "
    "cell_feature_matrix when the goal is the authors' published cell types rather "
    "than a fresh leiden. Call rds_inspect first to choose the assay. The heavy "
    "read + write runs LOCALLY; the model receives only the summary (paths + the "
    "schema of what was written). After it finishes, run inspect_input / "
    "inspect_structure on the output and continue the normal build. It can be slow "
    "on large objects (hundreds of MB).",
    {
        "input_path": Annotated[str, "Path to the input .rds / .RData."],
        "output": Annotated[str, "Output .h5ad path."],
        "assay": Annotated[
            str, "Assay to export (e.g. 'RNA', 'SCT'). '' = backend default."
        ],
        "x_layer": Annotated[
            str, "Layer/assay to map to AnnData X (e.g. 'counts', 'data'). '' = default."
        ],
        "reduced_dims": Annotated[
            list, "Embeddings to export (e.g. ['pca','umap']). Empty = all available."
        ],
        "no_spatial": Annotated[
            bool, "Skip inferred spatial coordinates in obsm. Default false (keep them)."
        ],
    },
)
async def rds_convert(args: dict[str, Any]) -> dict[str, Any]:
    reduced = [str(r).strip() for r in args.get("reduced_dims", []) if str(r).strip()]
    rr = commands.run_rds_convert(
        str(args["input_path"]),
        str(args["output"]),
        assay=(args.get("assay") or "").strip(),
        x_layer=(args.get("x_layer") or "").strip(),
        reduced_dims=reduced,
        no_spatial=bool(args.get("no_spatial", False)),
    )
    return _report(rr, f"rds2h5ad convert {args['input_path']} -> {args['output']}")


@tool(
    "rds_validate",
    "Read a produced .h5ad back through the rds2h5ad R backend (zellkonverter) "
    "and return a compact STRUCTURAL summary: cell and gene counts, the assay "
    "names with their dims, reduced-dim (embedding) names, obs and var column "
    "names, and — if present — the spatial coordinate dims. Schema and counts "
    "only; NO data values, so it needs no example stripping. Use it right after "
    "rds_convert as an independent check that the conversion carried the assays, "
    "embeddings and coordinates you expected (a natural companion to converting "
    "an analyzed .rds). Requires rds2h5ad on PATH (R + zellkonverter).",
    {"input_path": Annotated[str, "Path to the .h5ad file to read back."]},
)
async def rds_validate(args: dict[str, Any]) -> dict[str, Any]:
    rr = commands.run_rds_validate(str(args["input_path"]))
    return _report(rr, f"rds2h5ad validate {args['input_path']}")


# --- Pre-processing -------------------------------------------------------

@tool(
    "ingest_spatial",
    "Assemble a folder of RAW, on-disk spatial output bundles into one "
    "KaroSpace-ready .h5ad — the local twin of geo_build. Point input_path at a "
    "directory holding one or more raw bundles (or at a single bundle). Two vendor "
    "layouts are auto-detected per bundle: Xenium (a folder with "
    "cell_feature_matrix.h5 + cells.parquet / cells.csv.gz, the 10x Onboard "
    "Analysis layout) and MERSCOPE / MERFISH (a folder with cell_by_gene.csv + "
    "cell_metadata.csv, the Vizgen layout); a folder mixing both is fine. It "
    "discovers every bundle, builds raw counts into X, drops control probes (keep "
    "them with include_control), sets cell centroids into obsm['spatial'], tags "
    "each cell with a sample_id from its relative bundle path, and concatenates "
    "them. Use this when the researcher hands you a local Xenium/MERSCOPE export "
    "rather than an existing .h5ad/.zarr or a GEO accession. The read + assembly "
    "runs LOCALLY; you receive only aggregate counts (samples, cells, genes, "
    "per-sample sizes, per-platform bundle counts) — never a sample label, path, "
    "or coordinate. Optional min_counts / min_genes apply the same QC as qc_filter "
    "inline (off by default; the raw ingest is lossless unless you set them). After "
    "it finishes, run inspect_input / inspect_structure on the output and continue "
    "the normal build (it has raw counts + coordinates but no clustering yet, so "
    "run_preprocess next).",
    {
        "input_path": Annotated[
            str, "Directory of raw Xenium / MERSCOPE bundles (or one bundle folder)."
        ],
        "output": Annotated[str, "New output .h5ad path (never overwritten)."],
        "include_control": Annotated[
            bool, "Keep negative-control / blank probes. Default false (Gene Expression only)."
        ],
        "min_counts": Annotated[
            int, "Optional QC: drop cells below this many total counts. 0 = off (default)."
        ],
        "min_genes": Annotated[
            int, "Optional QC: drop cells expressing fewer genes than this. 0 = off (default)."
        ],
        "exclude": Annotated[
            list, "Skip a bundle whose folder name contains one of these substrings. Empty = keep all."
        ],
    },
)
async def ingest_spatial(args: dict[str, Any]) -> dict[str, Any]:
    exclude = [str(p).strip() for p in args.get("exclude", []) if str(p).strip()]
    rr = commands.run_ingest_spatial(
        str(args["input_path"]),
        str(args["output"]),
        include_control=bool(args.get("include_control", False)),
        min_counts=int(args.get("min_counts") or 0),
        min_genes=int(args.get("min_genes") or 0),
        exclude=exclude,
    )
    return _report(rr, f"ingest_spatial.py {args['input_path']} -> {args['output']}")


@tool(
    "qc_filter",
    "Drop low-quality cells from a spatial .h5ad on counts and/or detected "
    "genes — the standard raw-Xenium QC step (filter_cells min_counts=40, "
    "min_genes=15 in the reference pipelines). Raw panels carry a tail of "
    "near-empty cells (segmentation debris, tile-edge fragments) that add noise "
    "to clustering and DE; this removes them before the companion / export. Set "
    "at least one of min_counts / min_genes to a positive threshold. The read + "
    "filter runs LOCALLY; you receive only the aggregate before/after cell "
    "counts — never a per-cell total, coordinate, or identifier. Thresholds are "
    "a scientific choice: start from the panel-typical defaults and re-run if the "
    "researcher wants them stricter/looser. After it finishes, inspect_input the "
    "output and continue. Requires finite, nonnegative, integer-valued raw counts "
    "in X; invalid counts are rejected (qc_counts_required). Always choose "
    "a new output path: existing files and the input cannot be overwritten.",
    {
        "input_path": Annotated[str, "Input .h5ad with raw counts in X."],
        "output": Annotated[str, "New output .h5ad path (filtered); must not already exist."],
        "min_counts": Annotated[
            int, "Drop cells with fewer than this many total counts. 0 = off."
        ],
        "min_genes": Annotated[
            int, "Drop cells expressing fewer than this many genes. 0 = off."
        ],
    },
)
async def qc_filter(args: dict[str, Any]) -> dict[str, Any]:
    rr = commands.run_qc_filter(
        str(args["input_path"]),
        str(args["output"]),
        min_counts=int(args.get("min_counts") or 0),
        min_genes=int(args.get("min_genes") or 0),
    )
    return _report(rr, f"qc_filter.py {args['input_path']} -> {args['output']}")


@tool(
    "run_preprocess",
    "Add a transcriptomic clustering to a RAW matrix so it becomes ingestible. A "
    "freshly acquired file (e.g. from geo_build) has raw counts + coordinates but "
    "NO cell-type / cluster columns in obs — so a viewer can only be coloured "
    "gene-by-gene. This runs the standard scanpy path (normalize -> log1p -> HVG "
    "-> PCA -> neighbors -> UMAP -> leiden) LOCALLY, writing layers['counts'] (raw, "
    "preserved), layers['normalized'] (log1p, colour from this), obs['leiden'] "
    "(feeds --main-cell-annotation / --cell-annotations), and a 2D obsm['X_umap'] "
    "the viewer auto-detects (added only when the input has no UMAP; an existing "
    "one is kept). Call it when inspect shows no analysis-derived annotation column "
    "or no embedding with a 'umap' role hint. Returns an aggregate log only "
    "(cluster count + per-cluster sizes, key names) — no per-cell labels or "
    "values. Resolution is a scientific choice: the default is a starting point; "
    "re-run with a different resolution if the user wants finer/coarser clusters. "
    "Heavier spatial-domain methods (CellCharter) are out of scope here — those "
    "belong in a generated notebook the researcher runs.",
    {
        "input_path": Annotated[str, "Input .h5ad with raw counts in X."],
        "output": Annotated[str, "Output .h5ad path (clustered)."],
        "resolution": Annotated[
            float, "Leiden resolution; higher = more clusters. Default 1.0."
        ],
        "key": Annotated[str, "obs column name for the clustering. Default 'leiden'."],
    },
)
async def run_preprocess(args: dict[str, Any]) -> dict[str, Any]:
    resolution = float(args.get("resolution") or 1.0)
    key = (args.get("key") or "leiden").strip() or "leiden"
    rr = commands.run_preprocess(
        str(args["input_path"]),
        str(args["output"]),
        resolution=resolution,
        key=key,
    )
    return _report(rr, f"preprocess.py {args['input_path']} --resolution {resolution}")


@tool(
    "split_sections",
    "Split physically-separate tissue pieces on ONE capture into their own "
    "labelled sections. A single Xenium/Visium run often holds several pieces on "
    "the same slide (e.g. normal skin + keloid, or three replicate strips): they "
    "share one sample_id but sit millimetres apart, so a viewer keyed on sample_id "
    "crams them into one panel. This assigns each cell to its piece from the "
    "spatial coordinates alone and writes an obs column to use as --section-key. "
    "Method 'auto' (default) PROPOSES pieces from the empty "
    "gaps between them; require local visual confirmation before using the labels. "
    "Method 'kmeans' takes a known count k per group — "
    "use it only when the researcher tells you how many pieces a capture has. Set "
    "'within' to an existing grouping column (usually 'sample_id') so pieces are "
    "found per sample and never bleed across samples that share a coordinate "
    "frame; labels become '<group>__p1', '<group>__p2', … The heavy read runs "
    "LOCALLY; you receive only the aggregate result (pieces per group + per-piece "
    "cell counts), never a coordinate. Errors with 'spatial_coordinates_missing' "
    "if the file has no coordinates. After it finishes, inspect_input the output "
    "and export with --section-key set to this column only after local review. "
    "Supports .h5ad with obsm coordinates only. Preserve existing columns. "
    "Generated pieces must NEVER be used as biological pseudobulk replicates.",
    {
        "input_path": Annotated[str, "Input .h5ad with spatial coordinates in obsm."],
        "output": Annotated[str, "Output .h5ad path (with the new section column)."],
        "within": Annotated[
            str, "Existing obs column to split within (e.g. 'sample_id'). '' = whole file."
        ],
        "method": Annotated[
            str, "'auto' (gap detection, discovers the count) or 'kmeans' (fixed k)."
        ],
        "k": Annotated[int, "Pieces per group; used only when method='kmeans'. Else 0."],
        "key": Annotated[str, "obs column name to write. Default 'section'."],
        "coords_key": Annotated[str, "obsm key for coordinates. Default 'spatial'."],
    },
)
async def split_sections(args: dict[str, Any]) -> dict[str, Any]:
    method = (args.get("method") or "auto").strip() or "auto"
    key = (args.get("key") or "section").strip() or "section"
    coords_key = (args.get("coords_key") or "spatial").strip() or "spatial"
    rr = commands.run_split_sections(
        str(args["input_path"]),
        str(args["output"]),
        within=(args.get("within") or "").strip(),
        method=method,
        k=int(args.get("k") or 0),
        key=key,
        coords_key=coords_key,
    )
    return _report(rr, f"split_sections.py {args['input_path']} --method {method} -> {args['output']}")


@tool(
    "preview_sections",
    "Render a LOCAL visual preview of the section-split proposal — one image "
    "panel per capture group, each cell coloured by its proposed tissue piece — "
    "so the RESEARCHER can eyeball how many pieces there really are before that "
    "count becomes --section-key. Call this in the web/app surface right BEFORE "
    "you ask the researcher how many pieces they see: it runs the same gap "
    "detection as split_sections and streams the panels to their screen, turning "
    "the count question from a guess into something they can confirm by sight. "
    "The images are written and shown LOCALLY; you never receive them. You get "
    "back only the aggregate proposed piece count per group — no coordinate, no "
    "image, no sample ID. Use 'within' (usually 'sample_id') so pieces are found "
    "per capture. Supports .h5ad with obsm coordinates only. This renders a "
    "proposal for human review; it changes no file and writes no obs column — run "
    "split_sections once the researcher confirms the count.",
    {
        "input_path": Annotated[str, "Input .h5ad with spatial coordinates in obsm."],
        "output_dir": Annotated[
            str, "Local directory for the preview PNGs (e.g. '/karo/output/section_preview')."
        ],
        "within": Annotated[
            str, "Existing obs column to preview within (e.g. 'sample_id'). '' = whole file."
        ],
        "method": Annotated[
            str, "'auto' (gap detection, proposes the count) or 'kmeans' (fixed k)."
        ],
        "k": Annotated[int, "Pieces per group; used only when method='kmeans'. Else 0."],
        "coords_key": Annotated[str, "obsm key for coordinates. Default 'spatial'."],
    },
)
async def preview_sections(args: dict[str, Any]) -> dict[str, Any]:
    method = (args.get("method") or "auto").strip() or "auto"
    coords_key = (args.get("coords_key") or "spatial").strip() or "spatial"
    rr = commands.run_preview_sections(
        str(args["input_path"]),
        str(args["output_dir"]),
        within=(args.get("within") or "").strip(),
        method=method,
        k=int(args.get("k") or 0),
        coords_key=coords_key,
    )
    return _report(rr, f"preview_sections.py {args['input_path']} --method {method}")


@tool(
    "generate_notebook",
    "Write a parameterized preprocessing NOTEBOOK the researcher runs themselves, "
    "instead of clustering in-agent. Use this when the analysis is too heavy or "
    "too scientific to run headless — chiefly CellCharter spatial-domain detection "
    "(scvi-tools + torch, and a real choice of how many domains) — or when the "
    "researcher wants to own the clustering. The notebook is templated from "
    "schema-level params only (paths, the section-key column NAME, gene names, "
    "numbers); it reads no data, so nothing crosses the boundary. It contains "
    "normalize → leiden → (CellCharter spatial domains) → write annotated .h5ad, "
    "and ends with the exact karospace-agent build command to feed the result "
    "back. This is a HANDOFF: after writing it you cannot continue the build in "
    "this session — tell the user to run it and return with the annotated file. "
    "For the light, in-agent path (leiden only, no heavy deps) use run_preprocess "
    "instead.",
    {
        "input_path": Annotated[str, "Raw .h5ad the notebook will load."],
        "output": Annotated[str, "Notebook .ipynb path to write."],
        "section_key": Annotated[
            str, "obs column separating sections/samples (batch/library). '' if single section."
        ],
        "resolution": Annotated[float, "Leiden resolution. Default 1.0."],
        "genes": Annotated[list, "Genes of interest to note (informational). May be empty."],
        "organism": Annotated[str, "'Human' or 'Mouse'."],
        "include_cellcharter": Annotated[
            bool, "Include the CellCharter spatial-domain cells. Default true."
        ],
    },
)
async def generate_notebook(args: dict[str, Any]) -> dict[str, Any]:
    genes = [str(g).strip() for g in args.get("genes", []) if str(g).strip()]
    rr = commands.run_gen_notebook(
        str(args["input_path"]),
        str(args["output"]),
        section_key=(args.get("section_key") or "").strip(),
        resolution=float(args.get("resolution") or 1.0),
        genes=genes,
        organism=(args.get("organism") or "Human").strip() or "Human",
        include_cellcharter=bool(args.get("include_cellcharter", True)),
    )
    return _report(rr, f"gen_notebook.py {args['input_path']} -> {args['output']}")


@tool(
    "merge_sections",
    "Merge per-section .h5ad files into one KaroSpace-ready file, adding "
    "sample_id / condition / sample_batch. Use when the input is a single "
    "section stripped of sample metadata (the single-section trap) and the user "
    "supplies the sibling files. Returns the merge log (cell counts, obsm/layers "
    "keys) — no cell-level data.",
    {
        "sections": Annotated[
            list, "Each item 'sample_id:path[:condition]', one per section file."
        ],
        "output": Annotated[str, "Output merged .h5ad path."],
    },
)
async def merge_sections(args: dict[str, Any]) -> dict[str, Any]:
    sections = [str(s) for s in args["sections"]]
    rr = commands.run_merge(sections, args["output"])
    return _report(rr, f"merge_sections.py -> {args['output']}")


@tool(
    "run_companion",
    "Run the karospace-companion pre-processor (Rust) with the given arguments, "
    "e.g. ['prepare', '<in.h5ad>', '--delaunay', '--groupby', 'sample_id', "
    "'--output', '<enriched.h5ad>']. Use to add a spatial neighbor graph or "
    "baked-in analytics before export. Returns the companion log only.",
    {"args": Annotated[list, "Argument tokens passed straight to the binary."]},
)
async def run_companion(args: dict[str, Any]) -> dict[str, Any]:
    tokens = [str(a) for a in args["args"]]
    rr = commands.run_companion(tokens)
    return _report(rr, f"karospace-companion {' '.join(tokens)}")


# --- Export & package -----------------------------------------------------

@tool(
    "run_export",
    "Run the karospace export. Provide the input, the output path, and the full "
    "list of flag tokens you chose from the metadata (e.g. ['--section-key', "
    "'sample_id', '--main-cell-annotation', 'cell_type', '--feature-storage', "
    "'sidecar']). Returns exit code + stdout/stderr so you can read errors and "
    "iterate. A bad --section-key surfaces as a traceback ending in ValueError; "
    "read it and fix the flag. Pseudobulk requires an explicit "
    "--pseudobulk-replicate-annotation naming a real biological replicate; "
    "the local guard refuses generated tissue-piece columns. "
    "Does NOT return viewer contents.",
    {
        "input_path": Annotated[str, "Input .h5ad / .zarr path."],
        "output": Annotated[str, "Output viewer .html path."],
        "flags": Annotated[
            list, "CLI flag tokens (each flag and its value as separate items)."
        ],
    },
)
async def run_export(args: dict[str, Any]) -> dict[str, Any]:
    flags = [str(f) for f in args.get("flags", [])]
    check = commands.check_pseudobulk(args["input_path"], flags)
    if check is not None and not check.ok:
        return _report(check, "local pseudobulk replicate check")
    argv = [args["input_path"], "-o", args["output"], *flags]
    rr = commands.run_karospace(argv)
    return _report(rr, f"karospace {' '.join(argv)}")


@tool(
    "package_sidecar",
    "Package an existing sidecar viewer.html into a single-file .karospace plus "
    "its .loader.html (no recompute). Run after a successful sidecar export to "
    "produce the second deliverable. Runs `karospace package-sidecar <html> "
    "[-o <out.karospace>]`.",
    {
        "html_path": Annotated[str, "Path to the sidecar viewer.html."],
        "output": Annotated[str, "Output .karospace path, or '' for the default."],
    },
)
async def package_sidecar(args: dict[str, Any]) -> dict[str, Any]:
    argv = ["package-sidecar", args["html_path"]]
    out = (args.get("output") or "").strip()
    if out:
        argv += ["-o", out]
    rr = commands.run_karospace(argv, timeout=600)
    return _report(rr, f"karospace {' '.join(argv)}")


@tool(
    "validate_output",
    "Report existence, kind, and size of output artifacts so you can confirm the "
    "viewer actually wrote. Pass every path you expect (viewer.html, and for a "
    "sidecar also viewer.features.json + viewer.features/, and for a package the "
    ".karospace + .loader.html). Returns file metadata only, never contents.",
    {"paths": Annotated[list, "Paths to stat."]},
)
async def validate_output(args: dict[str, Any]) -> dict[str, Any]:
    paths = [str(p) for p in args.get("paths", [])]
    if not paths:
        return _result("No paths given.", is_error=True)
    return _result(stat_paths(paths))


ALL_TOOLS = [
    inspect_input,
    inspect_structure,
    check_readiness,
    cli_help,
    geo_manifest,
    geo_build,
    geo_fetch_file,
    rds_inspect,
    rds_convert,
    rds_validate,
    ingest_spatial,
    qc_filter,
    run_preprocess,
    split_sections,
    preview_sections,
    generate_notebook,
    merge_sections,
    run_companion,
    run_export,
    package_sidecar,
    validate_output,
]

TOOL_NAMES = [t.name for t in ALL_TOOLS]

# Fully-qualified names as the SDK addresses in-process MCP tools:
# mcp__<server>__<tool>. Used to auto-allow exactly these and nothing else.
ALLOWED_TOOL_NAMES = [f"mcp__{SERVER_NAME}__{name}" for name in TOOL_NAMES]


def build_server(boundary=None):
    from .privacy import Boundary, PRIVACY_INSTRUCTIONS
    boundary = boundary or Boundary(allow_local_paths=True)
    wrapped = []
    for definition in ALL_TOOLS:
        async def invoke(arguments, definition=definition):
            return await boundary.invoke(definition, arguments)
        wrapped.append(tool(definition.name, definition.description + PRIVACY_INSTRUCTIONS, definition.input_schema)(invoke))
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=wrapped)
