#!/usr/bin/env python
"""Add only X_umap from an existing representation; preserve source analysis."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile


def run(input_path, output, representation="X_pca", n_neighbors=15, min_dist=0.5, random_state=0):
    import anndata as ad
    import numpy as np

    source, output = Path(input_path), Path(output)
    if source.resolve() == output.resolve() or output.exists() or output.is_symlink():
        raise ValueError("embedding_output_exists: choose a new output file")
    if source.suffix.lower() != ".h5ad" or output.suffix.lower() != ".h5ad":
        raise ValueError("embedding_input_unsupported: use .h5ad files")
    if n_neighbors < 2 or not 0 <= min_dist <= 1 or not 0 <= random_state <= 2**32 - 1:
        raise ValueError("embedding_parameters_invalid")
    data = ad.read_h5ad(source)

    def matrix(key, dimensions=None):
        value = np.asarray(data.obsm[key])
        if (value.ndim != 2 or value.shape[0] != data.n_obs or value.shape[1] < 1
                or value.dtype.kind not in "iuf" or not np.isfinite(value).all()
                or (dimensions is not None and value.shape[1] != dimensions)):
            raise ValueError("embedding_representation_invalid")
        return value

    if "X_umap" in data.obsm:
        matrix("X_umap", 2)
        action = "existing embedding preserved"
    elif "umap" in data.obsm:
        data.obsm["X_umap"] = matrix("umap", 2).copy()
        action = "existing embedding copied to the standard key"
    else:
        if representation not in data.obsm:
            raise ValueError("embedding_representation_missing: select an existing PCA or latent representation")
        if data.n_obs < 3:
            raise ValueError("embedding_too_few_cells")
        latent = matrix(representation)
        import scanpy as sc

        # Build the graph in a separate, expression-free object. Only the final
        # embedding is copied back; even source uns/neighbors remain untouched.
        work = ad.AnnData(shape=(data.n_obs, 0))
        work.obsm["source"] = latent.copy()
        sc.pp.neighbors(work, use_rep="source", n_neighbors=min(n_neighbors, data.n_obs - 1),
                        random_state=random_state)
        sc.tl.umap(work, min_dist=min_dist, random_state=random_state,
                   init_pos="random" if data.n_obs < 4 else "spectral")
        data.obsm["X_umap"] = work.obsm["X_umap"].copy()
        matrix("X_umap", 2)
        action = "embedding computed from an existing representation"
    output.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(ad, "settings"):
        ad.settings.allow_write_nullable_strings = True
    with tempfile.TemporaryDirectory(prefix=".umap-", dir=output.parent) as directory:
        staged = Path(directory) / "result.h5ad"
        data.write_h5ad(staged, compression="gzip", convert_strings_to_categoricals=False)
        try:
            os.link(staged, output)
        except FileExistsError:
            raise ValueError("embedding_output_exists: choose a new output file")
    return {"cells": data.n_obs, "dimensions": 2, "action": action}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--representation", default="X_pca")
    parser.add_argument("--n-neighbors", type=int, default=15)
    parser.add_argument("--min-dist", type=float, default=0.5)
    parser.add_argument("--random-state", type=int, default=0)
    args = parser.parse_args()
    result = run(args.input, args.output, args.representation, args.n_neighbors, args.min_dist, args.random_state)
    print(f"UMAP: {result['action']}; {result['cells']} cells, 2 dimensions")


if __name__ == "__main__":
    main()
