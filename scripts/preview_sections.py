#!/usr/bin/env python
"""Render a LOCAL visual preview of the section-split proposal — one panel per
capture group — so a human can EYEBALL how many tissue pieces are really there
before that count is used as `--section-key`.

The point is to make the "how many pieces do you see?" question answerable by
sight instead of a bare number. It runs the SAME gap detection as
`split_sections` (in fact it calls straight into it) and draws each capture's
cells coloured by the proposed piece.

Boundary note: nothing here crosses to the model. The PNGs are written to a
local session directory and streamed to the local UI over the progress side
channel; the only thing forwarded is the AGGREGATE piece count per group. The
image markers carry a local file PATH and a group INDEX — never a coordinate and
never the group VALUE (the sample ID).

Usage
-----
    python scripts/preview_sections.py IN.h5ad --out-dir DIR \
        --within sample_id --method auto

Output
------
One PNG per group under --out-dir, plus two kinds of stdout marker line:

    KAROSPACE_PREVIEW_IMG {json}   local PNG path + aggregate meta, for the UI
    PREVIEW_SECTIONS_JSON {json}   aggregate summary, for the privacy layer
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import anndata as ad
import numpy as np

_HERE = Path(__file__).resolve().parent
IMG_MARKER = "KAROSPACE_PREVIEW_IMG "


def _load_split():
    """Load the sibling splitter so the preview and the real split agree exactly."""
    spec = importlib.util.spec_from_file_location("split_sections", _HERE / "split_sections.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def log(msg: str) -> None:
    print(f"[preview_sections] {msg}", flush=True)


def _group_masks(adata, within: str):
    """(group_label, boolean mask) per capture group, in first-seen order —
    mirroring split_sections so panel N lines up with the split's group N."""
    if within:
        col = adata.obs[within].astype(str)
        order = list(dict.fromkeys(col))
        return [(g, (col.values == g)) for g in order]
    return [("", np.ones(adata.n_obs, dtype=bool))]


def _render_panel(xy, labels, pieces, gi, out_path, max_points, dpi, rng):
    """Draw one capture's cells coloured by proposed piece. Subsampled for speed;
    the piece assignment itself is computed on every cell."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if len(xy) > max_points:
        idx = rng.choice(len(xy), size=max_points, replace=False)
        xy, labels = xy[idx], labels[idx]

    order = list(dict.fromkeys(labels.tolist()))
    cmap = plt.get_cmap("tab10" if len(order) <= 10 else "tab20")
    pt = 2.0 if len(xy) < 20_000 else 0.6

    fig, ax = plt.subplots(figsize=(5.0, 5.0), dpi=dpi)
    for i, label in enumerate(order):
        mask = labels == label
        # Strip the "<group>__" prefix so a sample ID never lands in the legend.
        ax.scatter(xy[mask, 0], xy[mask, 1], s=pt, linewidths=0,
                   color=cmap(i % cmap.N), label=str(label).split("__")[-1])
    ax.set_aspect("equal")
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(f"Capture group {gi + 1} — {pieces} piece(s) proposed", fontsize=11)
    ax.legend(title=f"{len(order)} pieces", markerscale=6, fontsize=8,
              title_fontsize=8, loc="upper right", framealpha=0.85)
    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="Input .h5ad with spatial coordinates in obsm.")
    ap.add_argument("--out-dir", required=True, help="Local directory for the PNG panels.")
    ap.add_argument("--coords-key", default="spatial", help="obsm key for coordinates. Default 'spatial'.")
    ap.add_argument("--within", default="", help="obs column to preview within (e.g. 'sample_id'). '' = whole file.")
    ap.add_argument("--method", choices=("auto", "kmeans"), default="auto",
                    help="auto = gap detection (proposes the count); kmeans = fixed K.")
    ap.add_argument("--k", type=int, default=0, help="Pieces per group (kmeans only).")
    ap.add_argument("--eps", type=float, default=None, help="DBSCAN radius (auto: estimated).")
    ap.add_argument("--min-samples", type=int, default=10, help="DBSCAN min neighbours. Default 10.")
    ap.add_argument("--gap-factor", type=float, default=30.0, help="auto eps = spacing * this. Default 30.")
    ap.add_argument("--min-cells", type=int, default=100, help="auto: absorb pieces smaller than this. Default 100.")
    ap.add_argument("--max-points", type=int, default=60_000, help="Cap points drawn per panel. Default 60000.")
    ap.add_argument("--dpi", type=int, default=110, help="PNG resolution. Default 110.")
    args = ap.parse_args()

    if args.method == "kmeans" and args.k < 1:
        raise SystemExit("--k must be >= 1 when --method kmeans")
    if Path(args.input).suffix.lower() != ".h5ad":
        raise SystemExit("split_input_unsupported: previewing requires .h5ad with obsm coordinates")

    split = _load_split()
    rng = np.random.default_rng(0)
    within = args.within.strip()
    log(f"loading {args.input}")
    adata = ad.read_h5ad(args.input)
    log(f"coords key: {args.coords_key}; within: {within or '(whole file)'}; method: {args.method}")

    # The exact splitter output — piece assignment and per-group counts.
    labels, report_groups = split.split(
        adata, args.coords_key, within, args.method, args.k,
        args.eps, args.min_samples, args.gap_factor, args.min_cells, rng)
    labels = np.asarray(labels)
    xy_all = split._coords(adata, args.coords_key)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    masks = _group_masks(adata, within)

    for gi, ((_, mask), report) in enumerate(zip(masks, report_groups)):
        pieces = int(report["pieces"])
        out_path = (out_dir / f"panel_{gi + 1}.png").resolve()
        _render_panel(xy_all[mask], labels[mask], pieces, gi, out_path,
                      args.max_points, args.dpi, rng)
        log(f"group {gi + 1}: {pieces} piece(s) -> {out_path}")
        # Local UI marker: a PATH + aggregate meta. No coordinate, no sample ID.
        print(IMG_MARKER + json.dumps({"path": str(out_path), "group": gi + 1, "pieces": pieces}), flush=True)

    # Aggregate summary for the privacy layer: piece counts only.
    summary = {
        "n_groups": len(report_groups),
        "n_panels": len(report_groups),
        "method": args.method,
        "groups": [{"pieces": int(g["pieces"])} for g in report_groups],
    }
    print("PREVIEW_SECTIONS_JSON " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
