#!/usr/bin/env python
"""Ingest a folder of raw spatial output bundles into one KaroSpace-ready .h5ad.

The GEO path (`geo_fetch.py build`) assembles AnnData from a *remote* accession.
This is its local twin: point it at a directory that holds one or more raw output
bundles sitting on disk and it builds the same minimal AnnData — raw counts in X,
control probes dropped, cell centroids in `obsm['spatial']` — concatenating every
bundle into one file with a per-cell `sample_id`.

Two vendor layouts are auto-detected per bundle:

* **Xenium** (10x Onboard Analysis): a folder with `cell_feature_matrix.h5` plus a
  per-cell table (`cells.parquet` / `cells.csv.gz` / `cells.csv`).
* **MERSCOPE / MERFISH** (Vizgen): a folder with `cell_by_gene.csv[.gz]` (counts)
  plus `cell_metadata.csv[.gz]`.

A folder holding both layouts (mixed platforms under one root) is fine — each
bundle is assembled by its own vendor core and concatenated together. The heavy
assembly is shared verbatim with the GEO builder (`_assemble_xenium_from_paths` /
`_assemble_merscope_from_paths` in geo_fetch.py), so a locally-ingested file and a
GEO-built one are structurally identical downstream.

Data-handling boundary
----------------------
Discovery and assembly run entirely LOCALLY. The bundle paths relative to the
input root become `sample_id` VALUES (the folder name for a single bundle)
inside the written file — they never cross to the model. The
only thing printed for the model is an AGGREGATE line:

    INGEST_SPATIAL_JSON {"n_samples": S, "n_cells": N, "n_genes": G,
                         "controls_dropped": true, "sample_sizes": [n1, n2, ...],
                         "platforms": {"xenium": a, "merscope": b},
                         "qc": {"n_before": N0, "n_after": N} | null}

— counts only; no sample label, path, coordinate, or per-cell value. Inspect the
written .h5ad with inspect_input / inspect_structure for the schema.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

JSON_MARKER = "INGEST_SPATIAL_JSON "

# Marker files that identify each vendor layout.
XENIUM_MATRIX = "cell_feature_matrix.h5"
XENIUM_CELLS = ("cells.parquet", "cells.csv.gz", "cells.csv")
MERSCOPE_COUNTS = ("cell_by_gene.csv", "cell_by_gene.csv.gz")
MERSCOPE_META = ("cell_metadata.csv", "cell_metadata.csv.gz")


def log(msg: str) -> None:
    print(msg, flush=True)


def _load_sibling(name: str):
    """Import a sibling script by path so we reuse its assembly/filter core."""
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _first_present(bundle: Path, names) -> Path | None:
    for name in names:
        candidate = bundle / name
        if candidate.exists():
            return candidate
    return None


def _bundle_kind(d: Path) -> str | None:
    """Return the vendor layout of directory `d`: 'xenium', 'merscope', or None."""
    if (d / XENIUM_MATRIX).exists() and _first_present(d, XENIUM_CELLS) is not None:
        return "xenium"
    if _first_present(d, MERSCOPE_COUNTS) is not None and _first_present(d, MERSCOPE_META) is not None:
        return "merscope"
    return None


def discover_bundles(root: Path) -> list[Path]:
    """Bundle directories under `root` (or `root` itself), sorted for stability.

    A bundle is any directory carrying a recognized vendor layout. We do not
    descend into a matched bundle (its own subfolders are vendor internals, not
    nested samples)."""
    if _bundle_kind(root) is not None:
        return [root]
    found: list[Path] = []
    for d in sorted(p for p in root.rglob("*") if p.is_dir()):
        # Skip anything already inside a discovered bundle.
        if any(d == b or b in d.parents for b in found):
            continue
        if _bundle_kind(d) is not None:
            found.append(d)
    return found


def _excluded(label: str, patterns: list[str]) -> bool:
    return any(pat and pat.lower() in label.lower() for pat in patterns)


def _assemble(geo, kind: str, bundle: Path, label: str, include_control: bool, log_lines):
    """Dispatch one bundle to its vendor assembler in geo_fetch."""
    if kind == "xenium":
        return geo._assemble_xenium_from_paths(
            label, bundle / XENIUM_MATRIX, _first_present(bundle, XENIUM_CELLS),
            include_control, log_lines)
    return geo._assemble_merscope_from_paths(
        label, _first_present(bundle, MERSCOPE_COUNTS), _first_present(bundle, MERSCOPE_META),
        include_control, log_lines)


def run(input_path: Path, output: Path, include_control: bool,
        min_counts: int, min_genes: int, exclude: list[str]) -> dict:
    if input_path.suffix.lower() in (".h5ad", ".zarr"):
        raise SystemExit(
            f"ingest_input_unsupported: {input_path.name} is already an assembled "
            "dataset. Point this at a directory of raw output bundles; use "
            "inspect_input for an existing .h5ad/.zarr."
        )
    if not input_path.exists() or not input_path.is_dir():
        raise SystemExit(f"ingest_no_bundles: {input_path} is not a directory.")
    if output.exists() or output.is_symlink():
        raise SystemExit("ingest_output_exists: choose a new output path; existing files are preserved.")

    bundles = discover_bundles(input_path)
    geo = _load_sibling("geo_fetch")
    log_lines: list[str] = []

    adatas = []
    sample_dicts = []
    platforms: dict[str, int] = {}
    for bundle in bundles:
        if _excluded(bundle.name, exclude):
            continue
        kind = _bundle_kind(bundle)
        # Relative paths are unique even for sample-a/outs and sample-b/outs.
        # Keep single-bundle and flat-directory labels unchanged.
        label = bundle.name if bundle == input_path else bundle.relative_to(input_path).as_posix()
        adatas.append(_assemble(geo, kind, bundle, label, include_control, log_lines))
        sample_dicts.append({"gsm": label})
        platforms[kind] = platforms.get(kind, 0) + 1

    if not adatas:
        raise SystemExit(
            "ingest_no_bundles: no raw spatial bundle found under "
            f"{input_path}. Expected a folder with either {XENIUM_MATRIX} + a "
            "per-cell table (Xenium) or cell_by_gene.csv + cell_metadata.csv (MERSCOPE)."
        )

    for line in log_lines:
        log(line)

    adata = geo._concat(adatas, sample_dicts, log_lines[-1:])
    sample_sizes = [int(a.n_obs) for a in adatas]

    qc = None
    if (min_counts or 0) > 0 or (min_genes or 0) > 0:
        qcmod = _load_sibling("qc_filter")
        adata, n_before, n_after = qcmod.filter_cells(adata, min_counts, min_genes)
        qc = {"n_before": int(n_before), "n_after": int(n_after)}
        log(f"qc filter: {n_before} -> {n_after} cells "
            f"(min_counts={min_counts}, min_genes={min_genes})")

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        import anndata

        if hasattr(anndata, "settings"):
            anndata.settings.allow_write_nullable_strings = True
    except Exception:
        pass
    # Publish a completed file without replacing a destination created by another
    # process during assembly. A sibling temp directory keeps the hard link on the
    # same filesystem and cleans up partial writes on failure.
    import os
    import tempfile

    with tempfile.TemporaryDirectory(prefix=".ingest-", dir=output.parent) as staging:
        staged = Path(staging) / "assembled.h5ad"
        adata.write_h5ad(staged, compression="gzip")
        try:
            os.link(staged, output)
        except FileExistsError:
            raise SystemExit("ingest_output_exists: choose a new output path; existing files are preserved.")

    summary = {
        "n_samples": len(adatas),
        "n_cells": int(adata.n_obs),
        "n_genes": int(adata.n_vars),
        "controls_dropped": not include_control,
        "sample_sizes": sample_sizes,
        "platforms": platforms,
        "qc": qc,
    }
    log("")
    log(f"wrote: {output}  ({adata.n_obs} cells x {adata.n_vars} genes)")
    log("obs columns: " + ", ".join(map(str, adata.obs.columns)))
    log("obsm keys: " + (", ".join(adata.obsm.keys()) or "(none)"))
    log("Next: inspect_input / inspect_structure on the written file for the schema.")
    log(JSON_MARKER + json.dumps(summary))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Assemble a folder of raw Xenium / MERSCOPE bundles into one .h5ad.")
    ap.add_argument("input", help="Directory of raw output bundles (or one bundle).")
    ap.add_argument("-o", "--output", required=True, help="New output .h5ad path (never overwritten).")
    ap.add_argument("--include-control", action="store_true",
                    help="Keep negative-control / blank probes (default: Gene Expression only).")
    ap.add_argument("--min-counts", type=int, default=0,
                    help="Optional QC: drop cells below this many total counts (0 = off).")
    ap.add_argument("--min-genes", type=int, default=0,
                    help="Optional QC: drop cells expressing fewer genes than this (0 = off).")
    ap.add_argument("--exclude", action="append", default=[],
                    help="Skip a bundle whose folder name contains this substring (repeatable).")
    args = ap.parse_args()
    run(Path(args.input), Path(args.output), args.include_control,
        args.min_counts, args.min_genes, args.exclude)
    return 0


if __name__ == "__main__":
    sys.exit(main())
