#!/usr/bin/env python
"""Report the STRUCTURE of an .h5ad / SpatialData .zarr that `karospace
--inspect-input` does not expose: the X matrix's dtype/format and whether its
values are all integers, the names+dtypes of layers, and the keys of obsm and
obsp (including whether a spatial neighbor graph is already present).

Why this exists
---------------
`karospace --inspect-input` returns obs columns + feature counts only — nothing
about layers, obsm, or obsp. Blind to those, the agent cannot tell whether:

  * a spatial neighbor graph (obsp spatial_connectivities/distances) already
    exists — so it cannot tell whether the companion still needs to build one;
  * X is raw counts or already normalized, and which layer (if any) holds the
    counts — so it cannot choose the display-normalization flags
    (--statistics-normalization / --statistics-counts-layer /
    --statistics-normalized-layer) and risks double-normalizing or washing out.

Data-handling boundary
----------------------
This prints SCHEMA and AGGREGATES only — never cell values. Specifically: matrix
dtype and storage format (dense/csr/csc), layer/obsm/obsp KEY names, obsm column
counts (a shape), and a single aggregate boolean per matrix (`all_integer`)
computed locally from a sample. No expression values, coordinates, or category
labels are emitted. The heavy read stays on this machine; only these structural
facts are meant to cross to the model.

Runs entirely locally on disk; uses backed/lazy reads so it never materializes
the full matrix.

Usage
-----
    python scripts/inspect_structure.py /path/file.h5ad
    python scripts/inspect_structure.py /path/store.zarr --table table
"""
import argparse
import sys

# numpy/h5py/zarr are imported lazily inside the probe functions: this script
# runs under the karospace scientific interpreter (which has them), but the
# numpy-free formatting/routing must stay importable without them.

# How many matrix values to sample for the integer check. Enough to be decisive,
# small enough to stay instant even on millions of cells.
INTEGER_SAMPLE_CAP = 200_000


def _fmt_from_encoding(enc: str) -> str:
    return {"csr_matrix": "csr", "csc_matrix": "csc"}.get(enc, enc or "sparse")


def _all_integer_from_1d(values) -> str:
    import numpy as np

    if values.size == 0:
        return "yes"  # empty / all-zero sparse block: nothing non-integer
    if not np.all(np.isfinite(values)):
        return "no"
    return "yes" if np.allclose(values, np.rint(values)) else "no"


# --- HDF5 (.h5ad) ----------------------------------------------------------

def _h5_matrix(node) -> dict | None:
    """Describe an X-like node: {format, dtype, all_integer}. None if absent."""
    import h5py
    import numpy as np

    if node is None:
        return None
    if isinstance(node, h5py.Dataset):  # dense
        dtype = node.dtype
        if dtype.kind in ("i", "u"):
            allint = "yes"
        else:
            # Read enough leading rows to reach the sample cap, no full load.
            ncol = node.shape[1] if node.ndim > 1 else 1
            nrow = max(1, INTEGER_SAMPLE_CAP // max(1, ncol))
            sample = np.asarray(node[:nrow]).ravel()
            allint = _all_integer_from_1d(sample)
        return {"format": "dense", "dtype": str(dtype), "all_integer": allint}
    # sparse group (csr/csc): the value array is `data`
    data = node["data"]
    dtype = data.dtype
    if dtype.kind in ("i", "u"):
        allint = "yes"
    else:
        allint = _all_integer_from_1d(np.asarray(data[:INTEGER_SAMPLE_CAP]))
    return {
        "format": _fmt_from_encoding(node.attrs.get("encoding-type", "")),
        "dtype": str(dtype),
        "all_integer": allint,
    }


def _h5_ncols(node) -> str:
    import h5py

    if isinstance(node, h5py.Dataset):
        return str(node.shape[1]) if node.ndim > 1 else "1"
    return "dataframe"  # obsm stored as a dataframe group (uncommon)


def probe_h5ad(path: str) -> dict:
    import h5py

    report: dict = {"path": path, "kind": "h5ad"}
    with h5py.File(path, "r") as f:
        report["X"] = _h5_matrix(f.get("X"))
        raw = f.get("raw")
        if raw is not None and "X" in raw:
            report["raw_X"] = _h5_matrix(raw["X"])
        report["layers"] = {
            name: _h5_matrix(f["layers"][name]) for name in f["layers"]
        } if "layers" in f else {}
        report["obsm"] = {
            name: _h5_ncols(f["obsm"][name]) for name in f["obsm"]
        } if "obsm" in f else {}
        report["obsp"] = sorted(f["obsp"].keys()) if "obsp" in f else []
    return report


# --- Zarr (.zarr) ----------------------------------------------------------

def _zarr_matrix(node) -> dict | None:
    import numpy as np
    import zarr

    if node is None:
        return None
    if isinstance(node, zarr.Array):  # dense
        dtype = node.dtype
        if dtype.kind in ("i", "u"):
            allint = "yes"
        else:
            ncol = node.shape[1] if node.ndim > 1 else 1
            nrow = max(1, INTEGER_SAMPLE_CAP // max(1, ncol))
            allint = _all_integer_from_1d(np.asarray(node[:nrow]).ravel())
        return {"format": "dense", "dtype": str(dtype), "all_integer": allint}
    data = node["data"]  # sparse group
    dtype = data.dtype
    if dtype.kind in ("i", "u"):
        allint = "yes"
    else:
        allint = _all_integer_from_1d(np.asarray(data[:INTEGER_SAMPLE_CAP]))
    return {
        "format": _fmt_from_encoding(node.attrs.get("encoding-type", "")),
        "dtype": str(dtype),
        "all_integer": allint,
    }


def _zarr_ncols(node) -> str:
    import zarr

    if isinstance(node, zarr.Array):
        return str(node.shape[1]) if node.ndim > 1 else "1"
    return "dataframe"


def probe_zarr(path: str, table: str = "") -> dict:
    import zarr

    root = zarr.open_group(path, mode="r")
    # SpatialData stores AnnData tables under tables/<key>; a bare AnnData .zarr
    # is the table itself.
    if "tables" in root:
        tables = root["tables"]
        key = table or (list(tables.keys())[0] if len(tables) else "")
        if not key:
            raise ValueError("no tables found in the .zarr store")
        node = tables[key]
        report_key = f"zarr (table={key})"
    else:
        node = root
        report_key = "zarr"

    report: dict = {"path": path, "kind": report_key}
    report["X"] = _zarr_matrix(node.get("X"))
    if "raw" in node and "X" in node["raw"]:
        report["raw_X"] = _zarr_matrix(node["raw"]["X"])
    report["layers"] = {
        name: _zarr_matrix(node["layers"][name]) for name in node["layers"]
    } if "layers" in node else {}
    report["obsm"] = {
        name: _zarr_ncols(node["obsm"][name]) for name in node["obsm"]
    } if "obsm" in node else {}
    report["obsp"] = sorted(node["obsp"].keys()) if "obsp" in node else []
    return report


# --- Formatting ------------------------------------------------------------

_SPATIAL_GRAPH_KEYS = ("spatial_connectivities", "spatial_distances")


def _fmt_matrix(m: dict | None) -> str:
    if not m:
        return "absent"
    return f"dtype={m['dtype']}, format={m['format']}, all_integer={m['all_integer']}"


def format_report(r: dict) -> str:
    lines = [f"structure: {r['path']}  [{r['kind']}]"]
    lines.append(f"X: {_fmt_matrix(r.get('X'))}")
    if r.get("raw_X"):
        lines.append(f"raw.X: {_fmt_matrix(r['raw_X'])}")
    layers = r.get("layers") or {}
    if layers:
        lines.append("layers:")
        for name, m in layers.items():
            lines.append(f"  - {name}: {_fmt_matrix(m)}")
    else:
        lines.append("layers: (none)")
    obsm = r.get("obsm") or {}
    lines.append(
        "obsm: " + (", ".join(f"{k} ({v} cols)" for k, v in obsm.items()) if obsm else "(none)")
    )
    obsp = r.get("obsp") or []
    lines.append("obsp: " + (", ".join(obsp) if obsp else "(none)"))
    has_graph = any(k in obsp for k in _SPATIAL_GRAPH_KEYS)
    lines.append(f"spatial_graph_present: {'yes' if has_graph else 'no'}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Structural probe (schema only).")
    ap.add_argument("input", help="Path to the .h5ad file or .zarr store.")
    ap.add_argument("--table", default="", help="For a .zarr with multiple tables, the table key.")
    args = ap.parse_args()

    path = args.input
    try:
        if path.rstrip("/").endswith(".zarr"):
            report = probe_zarr(path, args.table)
        else:
            report = probe_h5ad(path)
    except ImportError as e:
        print(f"structure probe unavailable: {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001 — report, don't crash the agent
        print(f"structure probe failed for {path}: {e}", file=sys.stderr)
        return 1

    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
