#!/usr/bin/env python
"""Split physically-separate tissue pieces on one capture into labelled sections.

A single Xenium/Visium run often carries several tissue pieces placed on the
same slide (e.g. "normal skin AND keloid", or three replicate strips). They land
in one .h5ad with one `sample_id`, but they are separated by millimetres of empty
space, so a viewer keyed on `sample_id` shows them all crammed into one panel.
This assigns each cell to its piece by looking at the spatial coordinates alone,
writing an obs column KaroSpace can use as `--section-key`.

Runs entirely locally on disk; no coordinate ever leaves the machine. What the
caller may forward is only the AGGREGATE result: how many pieces were found and
how many cells each holds.

Two detection methods:

  auto (default)  Density gap detection (DBSCAN). Proposes the number of pieces
                  from the large empty gaps between them — no need to know it in
                  advance. Local visual review is still required.
  kmeans          K-means with a known K per group. Use when the researcher has
                  looked at the capture and can say "there are 3 pieces here",
                  which is the classic workflow this replaces.

Detection runs independently WITHIN each `--within` group (typically the existing
`sample_id`), so two merged samples that share a coordinate frame never bleed into
each other. Piece labels are `<group>__p<n>` (or `p<n>` with no group), numbered
by descending cell count.

Usage
-----
    python scripts/split_sections.py IN.h5ad -o OUT.h5ad \
        --within sample_id --method auto --key section

    python scripts/split_sections.py IN.h5ad -o OUT.h5ad \
        --within sample_id --method kmeans --k 3 --key section
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anndata as ad
import numpy as np

MAX_NEIGHBOR_LINKS = 20_000_000

# Some inputs (e.g. GEO-assembled Xenium) carry pandas nullable StringArray obs
# columns that newer anndata refuses to write unless opted in. We only add a
# column and round-trip the rest, so preserve them rather than fail the write.
try:
    ad.settings.allow_write_nullable_strings = True
except Exception:  # older anndata without the setting
    pass


def log(msg: str) -> None:
    print(f"[split_sections] {msg}", flush=True)


def _coords(adata, key: str) -> np.ndarray:
    if key not in adata.obsm:
        # This exact phrase is mapped to a fixed diagnostic by the privacy layer.
        raise SystemExit(f"no spatial coordinates: obsm has no '{key}' "
                         f"(present: {list(adata.obsm.keys())})")
    xy = np.asarray(adata.obsm[key])
    if xy.ndim != 2 or xy.shape[1] < 2:
        raise SystemExit(f"no spatial coordinates: obsm['{key}'] is not 2-D points")
    xy = xy[:, :2].astype(float)
    if len(xy) == 0 or not np.isfinite(xy).all():
        raise SystemExit("split_invalid_coordinates: need nonempty, finite coordinates")
    return xy


def _typical_spacing(xy: np.ndarray, rng: np.random.Generator) -> float:
    """Median nearest-neighbour distance — the within-tissue cell spacing.

    Sampled (KD-tree on a subset) so this stays fast on hundreds of thousands of
    cells; the gaps between pieces dwarf this value, so a rough estimate is fine.
    """
    from scipy.spatial import cKDTree

    # Duplicated centroids are valid, but zero NN distances cannot define eps.
    xy = np.unique(xy, axis=0)
    n = xy.shape[0]
    if n < 2:
        return 0.0
    idx = rng.choice(n, size=min(n, 5000), replace=False)
    tree = cKDTree(xy)
    # k=2: self (distance 0) + the true nearest neighbour.
    dist, _ = tree.query(xy[idx], k=2)
    return float(np.median(dist[:, 1]))


def _absorb(xy: np.ndarray, labels: np.ndarray, keep: np.ndarray) -> np.ndarray:
    """Reassign every cell not in a kept cluster to its nearest kept cell, so
    stray specks and noise fold into the real piece next to them."""
    from scipy.spatial import cKDTree

    if keep.all():
        return labels
    if not keep.any():                       # nothing substantial: one piece
        return np.zeros(xy.shape[0], dtype=int)
    tree = cKDTree(xy[keep])
    _, nn = tree.query(xy[~keep], k=1)
    labels = labels.copy()
    labels[~keep] = labels[keep][nn]
    return labels


def _auto_labels(xy: np.ndarray, eps: float, min_samples: int, min_cells: int) -> np.ndarray:
    """DBSCAN to find the pieces, then absorb noise AND any cluster below
    `min_cells` into the nearest substantial piece, so a handful of stray cells
    never counts as its own section."""
    from sklearn.cluster import DBSCAN
    from scipy.spatial import cKDTree

    # sklearn materializes radius neighbours. Estimate their size before an
    # unexpectedly dense radius can exhaust RAM on a million-cell capture.
    probe = np.linspace(0, len(xy) - 1, min(len(xy), 1000), dtype=int)
    degree = cKDTree(xy).query_ball_point(xy[probe], eps, return_length=True)
    if float(np.mean(degree)) * len(xy) > MAX_NEIGHBOR_LINKS:
        raise SystemExit("split_density_too_high: use a smaller --eps or review a local subset")

    labels = DBSCAN(eps=eps, min_samples=min_samples).fit_predict(xy)
    uniq, counts = np.unique(labels[labels >= 0], return_counts=True)
    big = {u for u, c in zip(uniq, counts) if c >= min_cells}
    if not big:
        # Nothing clears the threshold (min_cells set too high, or genuinely
        # small pieces): keep every real cluster rather than collapsing to one.
        big = set(uniq.tolist())
    keep = np.array([lab in big for lab in labels])
    return _absorb(xy, labels, keep)


def _kmeans_labels(xy: np.ndarray, k: int) -> np.ndarray:
    from sklearn.cluster import KMeans

    if k < 1 or k > len(np.unique(xy, axis=0)):
        raise SystemExit("split_invalid_count: k must not exceed the distinct positions")
    return KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(xy)


def _rename_by_size(labels: np.ndarray, prefix: str) -> tuple[np.ndarray, list[int]]:
    """Relabel to `<prefix>p<n>`, n=1.. by descending cell count. Returns the new
    string labels and the ordered sizes."""
    uniq, counts = np.unique(labels, return_counts=True)
    order = np.argsort(-counts)                       # largest piece first
    rank = {uniq[o]: i for i, o in enumerate(order)}
    names = np.array([f"{prefix}p{rank[v] + 1}" for v in labels], dtype=object)
    sizes = [int(counts[o]) for o in order]
    return names, sizes


def split(adata, coords_key, within, method, k, eps, min_samples, gap_factor, min_cells, rng):
    xy_all = _coords(adata, coords_key)
    if within and within not in adata.obs:
        raise SystemExit(f"group column not found: obs has no '{within}'")

    if within:
        if adata.obs[within].isna().any():
            raise SystemExit("split_missing_groups: assign missing capture groups locally first")
        groups = adata.obs[within].astype(str)
        order = list(dict.fromkeys(groups))           # first-seen order, stable
    else:
        groups = None
        order = [""]

    out = np.empty(adata.n_obs, dtype=object)
    report_groups = []
    for gi, g in enumerate(order):
        mask = np.ones(adata.n_obs, dtype=bool) if groups is None else (groups.values == g)
        xy = xy_all[mask]
        prefix = "" if not g else f"{g}__"
        if method == "kmeans":
            labels = _kmeans_labels(xy, k)
            used_eps = None
        else:
            used_eps = eps if eps is not None else _typical_spacing(xy, rng) * gap_factor
            if len(xy) < min_samples or len(np.unique(xy, axis=0)) == 1:
                labels = np.zeros(len(xy), dtype=int)
            else:
                labels = _auto_labels(xy, used_eps, min_samples, min_cells)
        names, sizes = _rename_by_size(labels, prefix)
        out[mask] = names
        tag = f"group {gi + 1}" if within else "all cells"
        detail = f"k={k}" if method == "kmeans" else f"eps={used_eps:.1f}"
        log(f"{tag}: {len(sizes)} piece(s) ({detail}); sizes {sizes}")
        report_groups.append({"pieces": len(sizes), "sizes": sizes})
    return out, report_groups


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="Input .h5ad.")
    ap.add_argument("-o", "--output", required=True, help="Output .h5ad with the section column.")
    ap.add_argument("--coords-key", default="spatial", help="obsm key for coordinates. Default 'spatial'.")
    ap.add_argument("--within", default="", help="obs column to split within (e.g. 'sample_id'). '' = whole file.")
    ap.add_argument("--method", choices=("auto", "kmeans"), default="auto",
                    help="auto = gap detection (discovers piece count); kmeans = fixed K.")
    ap.add_argument("--k", type=int, default=0, help="Pieces per group (kmeans only).")
    ap.add_argument("--key", default="section", help="Output obs column name. Default 'section'.")
    ap.add_argument("--eps", type=float, default=None, help="DBSCAN radius in coord units (auto: estimated).")
    ap.add_argument("--min-samples", type=int, default=10, help="DBSCAN min neighbours. Default 10.")
    ap.add_argument("--gap-factor", type=float, default=30.0,
                    help="auto eps = typical cell spacing * this. Default 30.")
    ap.add_argument("--min-cells", type=int, default=100,
                    help="auto: pieces smaller than this are absorbed into the nearest piece. Default 100.")
    args = ap.parse_args()

    if args.method == "kmeans" and args.k < 1:
        raise SystemExit("--k must be >= 1 when --method kmeans")
    if (args.eps is not None and (not np.isfinite(args.eps) or args.eps <= 0)
            or not np.isfinite(args.gap_factor) or args.gap_factor <= 0
            or args.min_samples < 1 or args.min_cells < 1):
        raise SystemExit("split_invalid_parameters: radii and counts must be positive and finite")
    if Path(args.input).suffix.lower() != ".h5ad":
        raise SystemExit("split_input_unsupported: splitting requires .h5ad with obsm coordinates")
    if Path(args.input).resolve() == Path(args.output).resolve():
        raise SystemExit("split_existing_column: write a separate file to preserve the source")

    rng = np.random.default_rng(0)
    log(f"loading {args.input}")
    adata = ad.read_h5ad(args.input)
    if args.key in adata.obs:
        raise SystemExit("split_existing_column: choose a new column; existing labels are preserved")
    within = args.within.strip()
    log(f"coords key: {args.coords_key}; within: {within or '(whole file)'}; method: {args.method}")

    labels, report_groups = split(
        adata, args.coords_key, within, args.method, args.k,
        args.eps, args.min_samples, args.gap_factor, args.min_cells, rng)

    cats = sorted(set(labels))
    adata.obs[args.key] = np.asarray(labels)
    adata.obs[args.key] = adata.obs[args.key].astype("category")
    provenance = dict(adata.uns.get("karospace_section_split", {}))
    previous = list(provenance.get("section_keys", []))
    provenance["section_keys"] = list(dict.fromkeys([*previous, args.key]))
    adata.uns["karospace_section_split"] = provenance
    n_sections = len(cats)
    log(f"wrote section column '{args.key}' with {n_sections} categories")
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    adata.write_h5ad(args.output)
    log(f"wrote: {args.output}")

    # Machine-readable summary for the privacy layer: aggregate counts only,
    # never the group VALUES (sample IDs) or any coordinate.
    summary = {
        "n_sections": n_sections,
        "n_groups": len(report_groups),
        "method": args.method,
        "key": args.key,
        "groups": report_groups,
    }
    print("SPLIT_SECTIONS_JSON " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
