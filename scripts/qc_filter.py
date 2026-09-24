#!/usr/bin/env python
"""Filter low-quality cells out of a raw spatial .h5ad on counts / detected genes.

Raw Xenium (and other targeted panels) carry a long tail of near-empty cells —
segmentation debris, cells clipped at a tile edge — that add noise to clustering
and DE. The BaloMS / RRMAP2 pipelines drop them with the scanpy idiom
`filter_cells(min_counts=40)` + `filter_cells(min_genes=15)` before anything
else. This is that step, standalone.

Data-handling boundary
----------------------
The read + filter runs entirely LOCALLY. The only thing printed for the model is
an AGGREGATE line:

    QC_FILTER_JSON {"n_before": N, "n_after": M, "n_removed": N-M,
                    "min_counts": c, "min_genes": g}

— cell COUNTS only. No per-cell total, no coordinate, no identifier crosses.
Inspect the written file with inspect_input / inspect_structure for the schema.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

JSON_MARKER = "QC_FILTER_JSON "


def log(msg: str) -> None:
    print(msg, flush=True)


def _per_cell_stats(X):
    """Validate raw counts locally, then return totals and detected genes."""
    import numpy as np
    from scipy import sparse

    values = X.data if sparse.issparse(X) else np.asarray(X)
    invalid_counts = (
        "qc_counts_required: X must contain finite, nonnegative, integer-valued "
        "raw counts. Use the raw-count input; normalized expression is unsupported."
    )
    if values.dtype.kind not in "biuf":
        raise SystemExit(invalid_counts)
    # Bound validation memory without densifying sparse expression matrices.
    for start in range(0, values.size, 1_000_000):
        block = values.flat[start:start + 1_000_000]
        if (not np.isfinite(block).all() or (block < 0).any()
                or (values.dtype.kind == "f" and (block != np.floor(block)).any())):
            raise SystemExit(invalid_counts)

    if sparse.issparse(X):
        total = np.asarray(X.sum(axis=1, dtype=np.float64)).ravel()
        # nnz per row without densifying: count structural non-zeros per row.
        detected = np.asarray((X > 0).sum(axis=1)).ravel()
    else:
        arr = np.asarray(X)
        total = arr.sum(axis=1, dtype=np.float64).ravel()
        detected = (arr > 0).sum(axis=1).ravel()
    return total.astype(np.float64), detected.astype(np.int64)


def filter_cells(adata, min_counts: int = 0, min_genes: int = 0):
    """Return (filtered_adata, n_before, n_after), dropping cells below either
    threshold. A threshold of 0 is inactive. Mirrors scanpy's filter_cells but
    keeps the dependency surface to anndata + numpy + scipy."""
    import numpy as np

    n_before = int(adata.n_obs)
    total, detected = _per_cell_stats(adata.X)
    keep = np.ones(n_before, dtype=bool)
    if min_counts and min_counts > 0:
        keep &= total >= float(min_counts)
    if min_genes and min_genes > 0:
        keep &= detected >= int(min_genes)
    filtered = adata[keep].copy()
    return filtered, n_before, int(filtered.n_obs)


def run(input_path: Path, output: Path, min_counts: int, min_genes: int) -> dict:
    if input_path.suffix.lower() != ".h5ad":
        raise SystemExit(
            f"qc_input_unsupported: {input_path.name} is not a .h5ad. QC filtering "
            "reads an AnnData with counts in X; convert or ingest to .h5ad first."
        )
    if (min_counts or 0) <= 0 and (min_genes or 0) <= 0:
        raise SystemExit(
            "qc_no_threshold: set --min-counts and/or --min-genes to a positive "
            "value; with neither there is nothing to filter."
        )
    if input_path.resolve() == output.resolve():
        raise SystemExit("qc_output_same_as_input: choose a new output path to preserve the input.")
    if output.exists() or output.is_symlink():
        raise SystemExit("qc_output_exists: choose a new output path; existing files are preserved.")

    import anndata as ad

    adata = ad.read_h5ad(input_path)
    filtered, n_before, n_after = filter_cells(adata, min_counts, min_genes)
    log(f"filtered: {n_before} -> {n_after} cells "
        f"(min_counts={min_counts}, min_genes={min_genes})")

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        if hasattr(ad, "settings"):
            ad.settings.allow_write_nullable_strings = True
    except Exception:
        pass
    # Publish a completed file without replacing a destination created by
    # another process during filtering. A sibling temp directory keeps the
    # hard link on the same filesystem and cleans up partial writes on failure.
    import os
    import tempfile

    with tempfile.TemporaryDirectory(prefix=".qc-", dir=output.parent) as staging:
        staged = Path(staging) / "filtered.h5ad"
        filtered.write_h5ad(staged, compression="gzip")
        try:
            os.link(staged, output)
        except FileExistsError:
            raise SystemExit("qc_output_exists: choose a new output path; existing files are preserved.")

    summary = {"n_before": n_before, "n_after": n_after,
               "n_removed": n_before - n_after,
               "min_counts": int(min_counts or 0), "min_genes": int(min_genes or 0)}
    log(f"wrote: {output}")
    log("Next: inspect_input / inspect_structure on the written file for the schema.")
    log(JSON_MARKER + json.dumps(summary))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Filter low-quality cells from a spatial .h5ad.")
    ap.add_argument("input", help="Input .h5ad with raw counts in X.")
    ap.add_argument("-o", "--output", required=True, help="New output .h5ad path (never overwritten).")
    ap.add_argument("--min-counts", type=int, default=0,
                    help="Drop cells with fewer than this many total counts (0 = off).")
    ap.add_argument("--min-genes", type=int, default=0,
                    help="Drop cells expressing fewer than this many genes (0 = off).")
    args = ap.parse_args()
    run(Path(args.input), Path(args.output), args.min_counts, args.min_genes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
