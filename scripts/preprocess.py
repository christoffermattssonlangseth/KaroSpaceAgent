#!/usr/bin/env python
"""Local hands: create a transcriptomic clustering so a raw matrix becomes
KaroSpace-ingestible.

A freshly acquired dataset (e.g. from geo_fetch.py) has raw counts + coordinates
but NO cell-type / cluster columns in obs — so a viewer built from it can only be
coloured gene-by-gene. This runs the standard scanpy path
(normalize -> log1p -> HVG -> PCA -> neighbors -> leiden) LOCALLY to add a
`leiden` annotation, writing:

    layers['counts']      raw counts (preserved; X is restored to it at the end)
    layers['normalized']  library-size + log1p (the layer karospace should colour from)
    obs['leiden']         the clustering (feeds --main-cell-annotation / --cell-annotations)

Boundary: the heavy compute runs here, on the machine that holds the data. The
only thing printed is an AGGREGATE log — cell/gene counts, cluster count, and
per-cluster sizes (aggregate counts, like the cardinalities inspect already
emits). No per-cell labels, coordinates, or expression values are ever printed.

Clustering resolution is a scientific choice: this uses a reported default and is
meant to be re-run at a different --resolution, not treated as ground truth.
"""

from __future__ import annotations

import argparse
import sys

DEFAULT_KEY = "leiden"


def _log(lines: list[str], msg: str) -> None:
    lines.append(msg)


def _is_raw_counts(X) -> bool:
    """True if X holds integer counts (raw), False if it looks normalized.

    Reads only the stored nonzero values — never prints them.
    """
    import numpy as np
    import scipy.sparse as sp

    data = X.data if sp.issparse(X) else np.asarray(X).ravel()
    if data.size == 0:
        return True
    if data.min() < 0:
        return False
    # Sample to stay cheap on large matrices; all-integer is a structural fact.
    sample = data if data.size <= 1_000_000 else data[:: data.size // 1_000_000]
    return bool(np.all(np.mod(sample, 1) == 0))


def run_preprocess(
    input_path: str,
    output: str,
    *,
    method: str = "leiden",
    resolution: float = 1.0,
    n_neighbors: int = 15,
    n_pcs: int = 50,
    n_hvg: int = 2000,
    key: str = DEFAULT_KEY,
) -> list[str]:
    """Cluster `input_path` and write an ingestible file to `output`.

    Returns the aggregate log lines (also what the CLI prints)."""
    if method != "leiden":
        raise ValueError(f"unsupported method '{method}'; only 'leiden' is implemented")

    import anndata as ad
    import scanpy as sc

    log: list[str] = []
    _log(log, f"preprocess ({method}) {input_path}")
    adata = ad.read_h5ad(input_path)
    n_obs, n_vars = adata.shape
    _log(log, f"input: {n_obs} cells x {n_vars} genes")

    raw = _is_raw_counts(adata.X)
    if raw:
        if "counts" not in adata.layers:
            adata.layers["counts"] = adata.X.copy()
        sc.pp.normalize_total(adata, target_sum=1e4)
        sc.pp.log1p(adata)
        adata.layers["normalized"] = adata.X.copy()
        _log(log, "X was raw counts -> layers['counts'] kept, layers['normalized'] written (log1p)")
    else:
        if "normalized" not in adata.layers:
            adata.layers["normalized"] = adata.X.copy()
        _log(log, "X already normalized -> clustering on X as-is (layers['normalized'] ensured)")

    # HVG only helps when the panel is much larger than the target; a targeted
    # panel (e.g. Xenium ~300 genes) should use every gene.
    use_hvg = n_vars > n_hvg
    if use_hvg:
        sc.pp.highly_variable_genes(adata, n_top_genes=n_hvg)
        _log(log, f"HVG: selected {n_hvg} of {n_vars} genes")
    else:
        _log(log, f"HVG: skipped (panel {n_vars} <= target {n_hvg}; using all genes)")

    n_comps = max(2, min(n_pcs, n_vars - 1, n_obs - 1))
    sc.pp.pca(adata, n_comps=n_comps, use_highly_variable=use_hvg)
    n_neighbors = max(2, min(n_neighbors, n_obs - 1))
    sc.pp.neighbors(adata, n_neighbors=n_neighbors, n_pcs=n_comps)
    _log(log, f"PCA comps: {n_comps}   neighbors k: {n_neighbors}")

    try:
        sc.tl.leiden(
            adata, resolution=resolution, key_added=key,
            flavor="igraph", n_iterations=2, directed=False,
        )
    except TypeError:  # older scanpy without the igraph flavor kwargs
        sc.tl.leiden(adata, resolution=resolution, key_added=key)

    sizes = adata.obs[key].value_counts().sort_index()
    n_clusters = int(sizes.shape[0])
    _log(log, f"leiden resolution: {resolution} -> {n_clusters} clusters (obs['{key}'])")
    # Aggregate counts only (like cardinalities/missing counts inspect emits).
    _log(log, f"cluster sizes: {[int(v) for v in sizes.values]}")

    if raw:
        adata.X = adata.layers["counts"].copy()
        _log(log, "restored X to raw counts for ingestion (colour from layers['normalized'])")

    if hasattr(ad, "settings") and hasattr(ad.settings, "allow_write_nullable_strings"):
        ad.settings.allow_write_nullable_strings = True
    adata.write_h5ad(output, compression="gzip")
    _log(
        log,
        f"wrote: {output}  (obs adds '{key}'; layers: "
        f"{', '.join(sorted(adata.layers.keys())) or 'none'})",
    )
    return log


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Add a leiden clustering to a raw .h5ad for KaroSpace.")
    p.add_argument("input", help="Input .h5ad (raw counts).")
    p.add_argument("-o", "--output", required=True, help="Output .h5ad path.")
    p.add_argument("--method", default="leiden", help="Clustering method (only 'leiden' for now).")
    p.add_argument("--resolution", type=float, default=1.0, help="Leiden resolution (default 1.0).")
    p.add_argument("--n-neighbors", type=int, default=15, help="kNN graph neighbors (default 15).")
    p.add_argument("--n-pcs", type=int, default=50, help="PCA components (default 50).")
    p.add_argument("--n-hvg", type=int, default=2000, help="HVG target; skipped if panel <= this.")
    p.add_argument("--key", default=DEFAULT_KEY, help="obs column name for the clustering.")
    args = p.parse_args(argv)

    log = run_preprocess(
        args.input, args.output,
        method=args.method, resolution=args.resolution,
        n_neighbors=args.n_neighbors, n_pcs=args.n_pcs, n_hvg=args.n_hvg, key=args.key,
    )
    print("\n".join(log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
