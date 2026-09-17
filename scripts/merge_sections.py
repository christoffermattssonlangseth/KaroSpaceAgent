#!/usr/bin/env python
"""Merge per-section Xenium/spatial .h5ad files into one KaroSpace-ready AnnData.

Per-section exports often arrive stripped of sample-level metadata (obs has only
`orig.ident = "SeuratProject"`). This helper concatenates the sections and adds
the `sample_id` / `condition` / `sample_batch` columns KaroSpace needs for the
section key and filter chips — the metadata the merged file must carry.

Runs entirely locally on disk; no data leaves the machine.

Usage
-----
    python scripts/merge_sections.py \
        --section P1_L:/path/Xenium_P1_L_annotated_vF.h5ad:Lesional \
        --section P1_NL:/path/Xenium_P1_NL_annotated_vF.h5ad:Non-lesional \
        --output /path/Xenium_P1_LNL_merged.h5ad

Each --section is `sample_id:path[:condition]`. `sample_batch` defaults to
`sample_id`. Category order follows the order the sections are listed. Add more
--section entries (P2_L, P2_NL, …) to build a multi-patient file that can support
condition-level pseudobulk (needs ≥2 patients per condition).
"""
import argparse
import sys

import anndata as ad
import pandas as pd


def parse_section(spec):
    parts = spec.split(":")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(
            f"--section must be 'sample_id:path[:condition]', got {spec!r}"
        )
    sample_id, path = parts[0], parts[1]
    condition = parts[2] if len(parts) > 2 and parts[2] else None
    return {"sample_id": sample_id, "path": path, "condition": condition}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--section", action="append", type=parse_section, required=True,
                    metavar="sample_id:path[:condition]",
                    help="A section to merge; repeat for each file.")
    ap.add_argument("--output", required=True, help="Output merged .h5ad path.")
    args = ap.parse_args()

    sample_ids = [s["sample_id"] for s in args.section]
    conditions = [s["condition"] for s in args.section if s["condition"]]

    adatas, var_ref = [], None
    for s in args.section:
        a = ad.read_h5ad(s["path"])
        if var_ref is None:
            var_ref = list(a.var_names)
        elif set(a.var_names) != set(var_ref):
            n_shared = len(set(a.var_names) & set(var_ref))
            print(f"WARNING: {s['sample_id']} gene set differs "
                  f"({a.n_vars} vars, {n_shared} shared) — concat will union/NaN-fill.",
                  file=sys.stderr)
        a.obs["sample_id"] = s["sample_id"]
        a.obs["sample_batch"] = s["sample_id"]
        if s["condition"]:
            a.obs["condition"] = s["condition"]
        adatas.append(a)
        print(f"  {s['sample_id']}: {a.n_obs} cells"
              + (f", condition={s['condition']}" if s["condition"] else ""))

    merged = ad.concat(
        adatas, join="outer", merge="same",
        label="section", keys=sample_ids, index_unique="_",
    )

    # Categoricals with the order the sections were listed, so KaroSpace shows them
    # in a sensible order.
    merged.obs["sample_id"] = pd.Categorical(
        merged.obs["sample_id"], categories=sample_ids, ordered=True)
    merged.obs["sample_batch"] = pd.Categorical(
        merged.obs["sample_batch"], categories=sample_ids, ordered=True)
    if conditions:
        seen = list(dict.fromkeys(conditions))  # unique, order-preserving
        merged.obs["condition"] = pd.Categorical(
            merged.obs["condition"], categories=seen, ordered=True)

    print(f"\nmerged: {merged.n_obs} cells x {merged.n_vars} genes")
    print("obsm:", list(merged.obsm.keys()), "| layers:", list(merged.layers.keys()))
    merged.write_h5ad(args.output)
    print("wrote:", args.output)


if __name__ == "__main__":
    main()
