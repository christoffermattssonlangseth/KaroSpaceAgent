#!/usr/bin/env python
"""Local hands: generate a parameterized preprocessing notebook the researcher
runs themselves.

Some analyses are too heavy or too scientific to run headless in-agent — chiefly
CellCharter spatial-domain detection, which pulls in scvi-tools + torch and makes
a real biological choice (how many domains). For those, the agent emits a
notebook instead of running the compute: the researcher runs it on their own
machine/GPU, inspects and tweaks, then hands the annotated .h5ad back to the
build.

Boundary: this script only TEMPLATES a notebook from schema-level parameters
(paths, the section-key column NAME, gene names the user asked for, numeric
params). It never reads the dataset, so no data value can pass through it. The
notebook it writes runs entirely on the researcher's machine; nothing it computes
comes back to the model except, later, the schema of the annotated file.

The CellCharter / scVI API is version-sensitive — the generated cells follow the
canonical workflow and say so; the researcher adapts to their installed version.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CELLCHARTER_DOCS = "https://cellcharter.readthedocs.io/"


def _md(text: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": text.strip("\n").splitlines(keepends=True)}


def _code(text: str) -> dict:
    return {
        "cell_type": "code",
        "metadata": {},
        "execution_count": None,
        "outputs": [],
        "source": text.strip("\n").splitlines(keepends=True),
    }


def build_notebook(
    input_path: str,
    *,
    section_key: str = "",
    resolution: float = 1.0,
    genes: list[str] | None = None,
    organism: str = "Human",
    include_cellcharter: bool = True,
    domains_min: int = 2,
    domains_max: int = 15,
) -> dict:
    """Return an nbformat-v4 notebook dict. Pure templating — reads no data."""
    genes = genes or []
    annotated = str(Path(input_path).with_name(Path(input_path).stem + "_annotated.h5ad"))
    section_repr = repr(section_key) if section_key else "None"
    domains_line = (
        f"\nDOMAINS_RANGE = ({domains_min}, {domains_max})  "
        "# candidate #spatial domains for the stability selection"
        if include_cellcharter else ""
    )

    cells: list[dict] = [
        _md(f"""
# Prepare a dataset for KaroSpace

Cluster and (optionally) detect spatial domains for `{input_path}`, then write an
annotated file the KaroSpace build can ingest.

**This notebook runs on _your_ machine.** The data never leaves it; only the
schema of the file you produce is later shared with the agent. Resolution and the
number of spatial domains are scientific choices — the values below are starting
points, not ground truth. Re-run and compare.
"""),
        _md(f"""
## 1. Environment

Install into your analysis env (skip what you already have):

```
pip install scanpy leidenalg igraph
{"pip install cellcharter scvi-tools squidpy  # heavy; GPU recommended" if include_cellcharter else ""}
```
{(
    "The spatial-domain step is heavier than the in-agent path and a GPU makes "
    "scVI much faster. CellCharter's API is version-sensitive — if a call below "
    "differs from your installed version, check the docs: " + CELLCHARTER_DOCS
) if include_cellcharter else ""}
"""),
        _code(f"""
# --- Parameters (edit freely) ---
INPUT = {input_path!r}
ANNOTATED_OUTPUT = {annotated!r}
SECTION_KEY = {section_repr}        # obs column separating sections/samples (batch); None if single section
LEIDEN_RESOLUTION = {resolution}    # higher -> more transcriptomic clusters
GENES = {genes!r}                   # features of interest (informational)
ORGANISM = {organism!r}{domains_line}

import scanpy as sc
import anndata as ad
import numpy as np
import scipy.sparse as sp

adata = sc.read_h5ad(INPUT)
print(adata)
"""),
        _md("""
## 2. Normalize (preserve raw counts)

KaroSpace colours from a `normalized` layer and re-derives statistics from raw
counts, so keep both: raw counts in `layers['counts']` and log1p in
`layers['normalized']`.
"""),
        _code("""
def is_raw_counts(X):
    data = X.data if sp.issparse(X) else np.asarray(X).ravel()
    return data.size == 0 or (data.min() >= 0 and np.all(np.mod(data, 1) == 0))

if is_raw_counts(adata.X):
    adata.layers['counts'] = adata.X.copy()
    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)
    adata.layers['normalized'] = adata.X.copy()
    print("X was raw counts -> counts + normalized layers written")
else:
    adata.layers.setdefault('normalized', adata.X.copy())
    print("X already normalized -> clustering on X as-is")
"""),
        _md("""
## 3. Transcriptomic clustering (leiden)

The same path the in-agent `run_preprocess` runs — included here so you can tune
the resolution and see the effect before committing.
"""),
        _code("""
n_vars = adata.n_vars
if n_vars > 2000:
    sc.pp.highly_variable_genes(adata, n_top_genes=2000)
    use_hvg = True
else:
    use_hvg = False

sc.pp.pca(adata, n_comps=min(50, n_vars - 1, adata.n_obs - 1), use_highly_variable=use_hvg)
sc.pp.neighbors(adata, n_neighbors=15)
sc.tl.leiden(adata, resolution=LEIDEN_RESOLUTION, key_added='leiden')
print(adata.obs['leiden'].value_counts().sort_index())
"""),
    ]

    if include_cellcharter:
        cells += [
            _md(f"""
## 4. Spatial domains (CellCharter)

CellCharter finds tissue domains by clustering each cell's *neighbourhood*
representation. The canonical workflow: a scVI latent space, spatial neighbours,
neighbourhood aggregation, then GMM clustering whose cluster count is chosen by a
stability sweep over `DOMAINS_RANGE`. API is version-sensitive — see
{CELLCHARTER_DOCS}. Needs `obsm['spatial']` (a KaroSpace-ingestible file has it).
"""),
            _code("""
import scvi
import squidpy as sq
import cellcharter as cc

scvi.settings.seed = 0

# --- scVI latent space (trained on raw counts) ---
scvi.model.SCVI.setup_anndata(
    adata, layer='counts',
    batch_key=SECTION_KEY if SECTION_KEY in adata.obs else None,
)
model = scvi.model.SCVI(adata)
model.train(early_stopping=True)
adata.obsm['X_scVI'] = model.get_latent_representation()

# --- spatial neighbourhood graph (per section if we have one) ---
sq.gr.spatial_neighbors(
    adata, coord_type='generic', delaunay=True,
    library_key=SECTION_KEY if SECTION_KEY in adata.obs else None,
)
cc.gr.remove_long_links(adata)
cc.gr.aggregate_neighbors(adata, n_layers=3, use_rep='X_scVI', out_key='X_cellcharter')

# --- pick the number of domains by stability, then assign ---
autok = cc.tl.ClusterAutoK(n_clusters=DOMAINS_RANGE, max_runs=5)
autok.fit(adata, use_rep='X_cellcharter')
adata.obs['spatial_domain'] = autok.predict(adata, use_rep='X_cellcharter', k=autok.best_k)
adata.obs['spatial_domain'] = adata.obs['spatial_domain'].astype('category')
print(f"chosen #domains: {autok.best_k}")
print(adata.obs['spatial_domain'].value_counts().sort_index())
"""),
        ]

    write_cell = """
# Restore raw counts to X for ingestion (colour from layers['normalized']).
if 'counts' in adata.layers:
    adata.X = adata.layers['counts'].copy()

if hasattr(ad, 'settings') and hasattr(ad.settings, 'allow_write_nullable_strings'):
    ad.settings.allow_write_nullable_strings = True
adata.write_h5ad(ANNOTATED_OUTPUT, compression='gzip')
print('wrote', ANNOTATED_OUTPUT)
"""
    annotations = "leiden" + (", spatial_domain" if include_cellcharter else "")
    cells += [
        _md("## 5. Write the annotated file"),
        _code(write_cell),
        _md(f"""
## 6. Back to the build

Hand the annotated file to the agent — e.g.:

```
karospace-agent build {annotated} "grid by section, colour by {annotations.split(',')[0].strip()}"
```

The agent will inspect its schema, see the new annotation column(s) ({annotations}),
and choose export flags from there.
"""),
    ]

    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 4,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate a KaroSpace preprocessing notebook.")
    p.add_argument("input", help="Raw .h5ad the notebook will load.")
    p.add_argument("-o", "--output", required=True, help="Notebook .ipynb path to write.")
    p.add_argument("--section-key", default="", help="obs column separating sections/samples.")
    p.add_argument("--resolution", type=float, default=1.0, help="Leiden resolution (default 1.0).")
    p.add_argument("--genes", default="", help="Comma-separated genes of interest (informational).")
    p.add_argument("--organism", default="Human", help="Human / Mouse.")
    p.add_argument("--no-cellcharter", action="store_true", help="Omit the CellCharter spatial-domain cells.")
    p.add_argument("--domains-min", type=int, default=2, help="Min candidate #spatial domains.")
    p.add_argument("--domains-max", type=int, default=15, help="Max candidate #spatial domains.")
    args = p.parse_args(argv)

    genes = [g.strip() for g in args.genes.split(",") if g.strip()]
    nb = build_notebook(
        args.input,
        section_key=args.section_key,
        resolution=args.resolution,
        genes=genes,
        organism=args.organism,
        include_cellcharter=not args.no_cellcharter,
        domains_min=args.domains_min,
        domains_max=args.domains_max,
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(nb, indent=1))
    n_code = sum(1 for c in nb["cells"] if c["cell_type"] == "code")
    print(
        f"wrote notebook: {out}  ({len(nb['cells'])} cells, {n_code} code)  "
        f"cellcharter={'no' if args.no_cellcharter else 'yes'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
