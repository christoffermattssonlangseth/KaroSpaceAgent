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

CELLCHARTER_DOCS = "https://cellcharter.readthedocs.io/en/stable/"


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
    if include_cellcharter and not (2 <= domains_min < domains_max):
        raise ValueError("Choose at least two candidate domain counts, starting at 2 or higher.")
    domains_line = (
        f"\nDOMAINS = list(range({domains_min}, {domains_max} + 1))  "
        "# candidate domain counts for CellCharter stability selection"
        "\nPRIMARY_K = None  "
        "# REQUIRED: choose after reviewing stability and spatial plots"
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
pip install scanpy leidenalg igraph scikit-misc
{"pip install cellcharter scvi-tools squidpy scikit-learn torch  # heavy; GPU strongly recommended" if include_cellcharter else ""}
```
{(
    "The spatial-domain step is far heavier than the in-agent path: on ~1M+ cells "
    "scVI trains for hours, so run it on a machine with a GPU (CUDA or Apple MPS). "
    "It checkpoints after scVI and after aggregation — if a later cell fails, "
    "reload the checkpoint instead of retraining. CellCharter's API is "
    "version-sensitive — if a call below differs from your installed version, "
    "check the docs: " + CELLCHARTER_DOCS
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

# Apply before EVERY write, including intermediate checkpoints.
if hasattr(ad, 'settings') and hasattr(ad.settings, 'allow_write_nullable_strings'):
    ad.settings.allow_write_nullable_strings = True

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
representation: an scVI latent space → a spatial neighbour graph → multi-hop
neighbourhood aggregation → GMM clustering over a range of domain counts. API is
version-sensitive — see {CELLCHARTER_DOCS}. Needs `obsm['spatial']` (a
KaroSpace-ingestible file has it).

**This stage is heavy and long** — on ~1M+ cells scVI training runs for hours
(a GPU is strongly recommended) and the aggregation is memory-hungry. The cells
below therefore:
- select scVI HVGs independently from raw `counts` using seurat_v3 when there
  are more than 5,000 genes; retain all genes for smaller targeted panels,
- **write checkpoints** after scVI and after aggregation so a crash never costs
  the whole run — re-run from the last checkpoint,
- **aggregate per library** (the spatial graph is block-diagonal by section, so
  this is identical to the full-object call but caps peak RAM at the largest
  section — the naive full-object call OOMs at ~1.4M cells),
- use CellCharter ClusterAutoK stability selection and local spatial plots;
  require you to set `PRIMARY_K` before saving the primary annotation.

After a crash, load the `.post_scvi.h5ad` or `.post_aggregate.h5ad` checkpoint
into `adata` after the parameter/import cell and resume at the next stage.
Keep `SECTION_KEY` unchanged. Do not rerun completed training or aggregation.
The documented API is at {CELLCHARTER_DOCS}generated/cellcharter.tl.ClusterAutoK.html.
"""),
            _code("""
import scvi
import squidpy as sq
import cellcharter as cc

scvi.settings.seed = 0
LIBRARY_KEY = SECTION_KEY if (SECTION_KEY and SECTION_KEY in adata.obs) else None
if LIBRARY_KEY:
    if adata.obs[LIBRARY_KEY].isna().any():
        raise ValueError('Assign missing library labels locally before training.')
    adata.obs[LIBRARY_KEY] = adata.obs[LIBRARY_KEY].astype('category')

# Checkpoint paths derived from the output — reload to skip a finished stage.
from pathlib import Path as _P
_SCVI_CKPT = str(_P(ANNOTATED_OUTPUT).with_suffix('')) + '.post_scvi.h5ad'
_AGG_CKPT = str(_P(ANNOTATED_OUTPUT).with_suffix('')) + '.post_aggregate.h5ad'

# Pick the fastest accelerator actually available.
try:
    import torch
    ACCELERATOR = ('cuda' if torch.cuda.is_available()
                   else 'mps' if torch.backends.mps.is_available() else 'cpu')
except Exception:
    ACCELERATOR = 'auto'
print('scVI accelerator:', ACCELERATOR)
"""),
            _code("""
# --- scVI latent space (trained on seurat_v3 HVGs of the raw counts) ---
if 'X_scVI' not in adata.obsm:
    if 'counts' not in adata.layers or not is_raw_counts(adata.layers['counts']):
        raise ValueError('scVI requires a verified raw counts layer; prepare it locally first.')
    # Never reuse the Leiden HVG mask, which was selected from normalized X.
    if adata.n_vars > 5000:
        hvg = sc.pp.highly_variable_genes(
            adata, flavor='seurat_v3', n_top_genes=5000, subset=False,
            layer='counts', batch_key=LIBRARY_KEY, inplace=False,
        )
        scvi_mask = hvg['highly_variable'].to_numpy()
    else:
        scvi_mask = np.ones(adata.n_vars, dtype=bool)
    adata.var['scvi_highly_variable'] = scvi_mask
    adata_hvg = adata[:, scvi_mask].copy()
    scvi.model.SCVI.setup_anndata(
        adata_hvg, layer='counts',
        batch_key=LIBRARY_KEY if LIBRARY_KEY in adata_hvg.obs else None,
    )
    model = scvi.model.SCVI(adata_hvg)
    model.train(max_epochs=1000, early_stopping=True, accelerator=ACCELERATOR)
    adata.obsm['X_scVI'] = model.get_latent_representation()
    del model, adata_hvg
    adata.write_h5ad(_SCVI_CKPT)      # checkpoint: scVI is the expensive part
    print('wrote scVI checkpoint:', _SCVI_CKPT)
else:
    print('X_scVI already present — skipping training')
"""),
            _code("""
# --- spatial neighbour graph (per section; no edges between sections) ---
sq.gr.spatial_neighbors(
    adata, coord_type='generic', delaunay=True, library_key=LIBRARY_KEY,
)
cc.gr.remove_long_links(adata)
"""),
            _code("""
# --- neighbourhood aggregation, per library (memory-safe) ---
# The full-object cc.gr.aggregate_neighbors densifies multi-hop graph powers and
# OOMs at ~1.4M cells. The graph is block-diagonal by LIBRARY_KEY, so aggregating
# each section separately is mathematically identical but caps peak RAM.
import time

N_LAYERS, USE_REP, OUT_KEY = 3, 'X_scVI', 'X_cellcharter'
latent_dim = adata.obsm[USE_REP].shape[1]
X_out = np.full((adata.n_obs, (N_LAYERS + 1) * latent_dim), np.nan, dtype=np.float32)

if LIBRARY_KEY:
    libraries = list(adata.obs[LIBRARY_KEY].unique())
    masks = [(str(lib), (adata.obs[LIBRARY_KEY] == lib).values) for lib in libraries]
else:
    masks = [('all', np.ones(adata.n_obs, dtype=bool))]
print(f'aggregating across {len(masks)} librar(y/ies); output shape {X_out.shape}')

for i, (lib, mask) in enumerate(masks, 1):
    t0 = time.time()
    # Copy only the graph and latent vectors, not every expression layer/raw.
    sub = ad.AnnData(X=sp.csr_matrix((int(mask.sum()), 0)))
    sub.obsm[USE_REP] = adata.obsm[USE_REP][mask].copy()
    sub.obsp['spatial_connectivities'] = adata.obsp['spatial_connectivities'][mask][:, mask].copy()
    cc.gr.aggregate_neighbors(
        sub, n_layers=N_LAYERS, use_rep=USE_REP, out_key=OUT_KEY,
        connectivity_key='spatial_connectivities', aggregations='mean',
    )
    X_out[mask] = sub.obsm[OUT_KEY].astype(np.float32)
    print(f'  [{i}/{len(masks)}] {lib}: n={int(mask.sum()):>7d}  {time.time()-t0:6.1f}s')
    del sub

adata.obsm[OUT_KEY] = X_out

# --- correctness checks (fail loud if per-library indexing is wrong) ---
assert not np.isnan(X_out).any(axis=1).any(), 'some cells got no aggregated row'
assert np.isfinite(X_out).all(), 'non-finite values in X_cellcharter'
# Layer 0 (0-hop) must equal each cell's own scVI vector.
assert np.allclose(X_out[:, :latent_dim], adata.obsm[USE_REP].astype(np.float32), atol=1e-5), \\
    'layer-0 slice != X_scVI — per-library output is misaligned'

adata.write_h5ad(_AGG_CKPT)          # checkpoint: aggregation is memory-risky
print('wrote aggregation checkpoint:', _AGG_CKPT)
"""),
            _code("""
# --- CellCharter stability selection (documented ClusterAutoK API) ---
if max(DOMAINS) + 1 >= adata.n_obs:
    raise ValueError('Choose a smaller domain-count range for this dataset.')
autok = cc.tl.ClusterAutoK(
    n_clusters=(min(DOMAINS), max(DOMAINS)), max_runs=5,
    # Use CellCharter's default GaussianMixture estimator (fit accepts arrays).
    model_params={'batch_size': 1024,
                  'trainer_params': {'accelerator': ACCELERATOR, 'devices': 1, 'enable_progress_bar': False}},
)
autok.fit(adata, use_rep='X_cellcharter')
autok.save(str(_P(ANNOTATED_OUTPUT).with_suffix('')) + '.cellcharter_models')
cc.pl.autok_stability(autok)
print('Stability suggests:', autok.best_k, '— review before choosing PRIMARY_K.')

for k in DOMAINS:
    adata.obs[f'CellCharter_{k}'] = autok.predict(adata, use_rep='X_cellcharter', k=k)
# Subsample only the local display; clustering and saved annotations use ALL cells.
plot_indices = np.random.default_rng(0).choice(adata.n_obs, min(20_000, adata.n_obs), replace=False)
sc.pl.embedding(adata[plot_indices], basis='spatial',
                color=[f'CellCharter_{k}' for k in DOMAINS], ncols=3)
"""),
            _md("""
### Choose the primary domain count
Review the stability curve and spatial plots above. Edit `PRIMARY_K` in the next
cell, then run it and the final write cell. A suggestion is not biological ground
truth. Run All intentionally stops here until you make a choice.
"""),
            _code("""
# Set PRIMARY_K to a reviewed candidate, for example PRIMARY_K = 6.
if PRIMARY_K is None or PRIMARY_K not in DOMAINS:
    raise ValueError('Set PRIMARY_K to a reviewed candidate before writing spatial_domain.')
adata.obs['spatial_domain'] = adata.obs[f'CellCharter_{PRIMARY_K}'].astype('category')
adata.uns['karospace_cellcharter_primary_k'] = int(PRIMARY_K)
print(f'Chosen primary spatial_domain: {PRIMARY_K}')
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
    if include_cellcharter:
        write_cell = """
if (PRIMARY_K is None or 'spatial_domain' not in adata.obs
        or adata.uns.get('karospace_cellcharter_primary_k') != PRIMARY_K):
    raise ValueError('Review and apply the primary domain selection before saving.')
""" + write_cell
    annotations = "leiden" + (", spatial_domain, CellCharter_<k>" if include_cellcharter else "")
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
