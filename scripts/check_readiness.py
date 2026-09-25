#!/usr/bin/env python
"""Local, chunked readiness checks. Print only fixed codes and aggregate counts."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import tempfile

CHUNK = 262_144


@contextmanager
def open_table(path, table=""):
    if Path(path).suffix.lower() == ".h5ad":
        import h5py
        with h5py.File(path, "r") as root:
            yield root
    elif Path(path).suffix.lower() == ".zarr":
        import zarr
        root = zarr.open_group(str(path), mode="r")
        if "tables" in root:
            tables = root["tables"]
            keys = list(tables.keys())
            if not table and len(keys) != 1:
                raise ValueError("table_selection_required")
            root = tables[table or keys[0]]
        yield root
    else:
        raise ValueError("input_unsupported")


def blocks(array):
    """Bound each read even for very wide dense expression matrices."""
    import numpy as np
    if len(array.shape) == 1:
        for start in range(0, array.shape[0], CHUNK):
            yield np.asarray(array[start:start + CHUNK])
    elif len(array.shape) == 2:
        width = max(1, min(array.shape[1], CHUNK))
        rows = max(1, CHUNK // width)
        for row in range(0, array.shape[0], rows):
            for col in range(0, array.shape[1], width):
                yield np.asarray(array[row:row + rows, col:col + width]).ravel()
    else:
        raise ValueError("invalid_matrix")


def matrix_info(node):
    import numpy as np
    if hasattr(node, "shape"):
        shape = tuple(node.shape)
        values = node
        size = int(np.prod(shape)) * node.dtype.itemsize
    else:
        encoding = node.attrs.get("encoding-type", "")
        shape = tuple(node.attrs.get("shape", ()))
        if encoding not in ("csr_matrix", "csc_matrix") or len(shape) != 2:
            raise ValueError("invalid_matrix")
        values = node["data"]
        indices, pointers = node["indices"], node["indptr"]
        major, minor = shape if encoding == "csr_matrix" else shape[::-1]
        if (len(values.shape) != 1 or indices.shape != values.shape
                or pointers.shape != (major + 1,)
                or int(pointers[0]) != 0 or int(pointers[-1]) != values.shape[0]
                or indices.dtype.kind not in "iu" or pointers.dtype.kind not in "iu"):
            raise ValueError("invalid_matrix")
        previous = 0
        for block in blocks(pointers):
            if (block < previous).any() or (block[1:] < block[:-1]).any() or (block > values.shape[0]).any():
                raise ValueError("invalid_matrix")
            if block.size:
                previous = int(block[-1])
        for block in blocks(indices):
            if (block < 0).any() or (block >= minor).any():
                raise ValueError("invalid_matrix")
        size = sum(a.size * a.dtype.itemsize for a in (values, indices, pointers))
    if len(shape) != 2 or any(int(n) < 1 for n in shape) or values.dtype.kind not in "biuf":
        raise ValueError("invalid_matrix")
    finite = nonnegative = integer = True
    for block in blocks(values):
        finite = finite and bool(np.isfinite(block).all())
        nonnegative = nonnegative and bool((block >= 0).all())
        integer = integer and bool((block == np.floor(block)).all())
    return {"cells": int(shape[0]), "genes": int(shape[1]), "storage_bytes": int(size),
            "finite": finite, "raw_counts": finite and nonnegative and integer}


def section_info(node, cells):
    import numpy as np
    categorical = not hasattr(node, "shape")
    values = node["codes"] if categorical else node
    if values.shape != (cells,):
        raise ValueError("invalid_section")
    seen, missing = set(), 0
    for block in blocks(values):
        if categorical:
            if (block < -1).any() or (block >= node["categories"].shape[0]).any():
                raise ValueError("invalid_section")
            absent = block == -1
        elif values.dtype.kind in "f":
            absent = ~np.isfinite(block)
        else:
            absent = np.array([v is None or v == "" or v == b"" for v in block])
        missing += int(absent.sum())
        if len(seen) <= 10_000:
            seen.update(np.unique(block[~absent]).tolist())
            if len(seen) > 10_000:
                seen = set(range(10_001))  # retain only the high-cardinality indication
    return min(len(seen), 10_001), missing


def available_memory():
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except ImportError:
        try:
            return int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
        except (ValueError, OSError, AttributeError):
            return None


def check(input_path, output_dir, section_key="", coords_key="spatial", counts_layer="",
          require_counts=False, table="", *, require_spatial=True, spatial_x="", spatial_y="",
          coordinate_mode="strict"):
    import numpy as np
    errors, warnings = [], []
    report = {"errors": errors, "warnings": warnings}
    try:
        with open_table(input_path, table) as root:
            matrix = matrix_info(root["X"])
            report.update(cells=matrix["cells"], genes=matrix["genes"], raw_counts=matrix["raw_counts"])
            for key, expected in (("obs", matrix["cells"]), ("var", matrix["genes"] )):
                frame = root[key]
                index = frame[frame.attrs.get("_index", "_index")]
                if index.shape != (expected,):
                    errors.append("metadata_shape_mismatch")
            if not matrix["finite"]:
                errors.append("nonfinite_expression")
            if counts_layer:
                counts = matrix_info(root["layers"][counts_layer])
                if (counts["cells"], counts["genes"]) != (matrix["cells"], matrix["genes"]):
                    errors.append("counts_shape_mismatch")
                report["raw_counts"] = counts["raw_counts"]
            if require_counts and not report["raw_counts"]:
                errors.append("raw_counts_required")
            if require_spatial:
                # Match the local readers' established fallback order. Split
                # and preview remain strict about their explicit obsm key.
                embeddings = root.get("obsm", {})
                if coordinate_mode == "export" and coords_key not in embeddings:
                    for key in ("spatial", "X_spatial", "spatial_coords", "X_spatial_coords", "Spatial", "spatialcoords"):
                        node = embeddings.get(key)
                        if hasattr(node, "shape") and len(node.shape) == 2 and node.shape[1] >= 2:
                            coords_key = key
                            break
                elif coordinate_mode == "companion":
                    coords_key = next((key for key in ("spatial", "X_spatial") if key in embeddings), "spatial")
                    if coords_key not in embeddings:
                        for x, y in (("array_col", "array_row"), ("pxl_col_in_fullres", "pxl_row_in_fullres"), ("x", "y")):
                            if x in root["obs"] and y in root["obs"]:
                                spatial_x, spatial_y = x, y
                                break
                if spatial_x or spatial_y:
                    coords = [root["obs"].get(key) for key in (spatial_x, spatial_y)]
                    if any(node is None or not hasattr(node, "shape")
                           or node.shape != (matrix["cells"],) or node.dtype.kind not in "iuf"
                           for node in coords):
                        errors.append("invalid_coordinates")
                    elif any(not np.isfinite(block).all() for node in coords for block in blocks(node)):
                        errors.append("invalid_coordinates")
                else:
                    coords = root.get("obsm", {}).get(coords_key)
                    if coords is None or not hasattr(coords, "shape"):
                        errors.append("spatial_coordinates_missing")
                    elif (len(coords.shape) != 2 or coords.shape[0] != matrix["cells"]
                          or coords.shape[1] < 2 or coords.dtype.kind not in "iuf"):
                        errors.append("invalid_coordinates")
                    elif any(not np.isfinite(block).all() for block in blocks(coords)):
                        errors.append("invalid_coordinates")
            if section_key:
                if "obs" not in root or section_key not in root["obs"]:
                    errors.append("section_key_missing")
                else:
                    groups, missing = section_info(root["obs"][section_key], matrix["cells"])
                    report.update(section_groups=groups, section_missing=missing)
                    if missing:
                        errors.append("section_labels_missing")
                    if groups > 10_000:
                        warnings.append("section_cardinality_high")
            elif require_spatial:
                warnings.append("section_key_not_selected")
            report["estimated_memory_bytes"] = matrix["storage_bytes"] * 3 + matrix["cells"] * 256
            report["estimated_output_bytes"] = max(matrix["storage_bytes"] * 4, 64 * 1024**2)
    except ValueError as exc:
        code = str(exc)
        errors.append(code if code in {"table_selection_required", "input_unsupported", "invalid_matrix", "invalid_section"}
                      else "input_unreadable")
    except (KeyError, OSError, TypeError, ImportError):
        errors.append("input_unreadable")
    parent = Path(output_dir).expanduser().absolute()
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    try:
        with tempfile.TemporaryFile(dir=parent):
            pass
        report["free_disk_bytes"] = shutil.disk_usage(parent).free
        if report["free_disk_bytes"] < report.get("estimated_output_bytes", 0):
            warnings.append("disk_estimate_exceeds_free_space")
    except OSError:
        errors.append("output_not_writable")
    memory = available_memory()
    if memory is None:
        warnings.append("memory_available_unknown")
    else:
        report["available_memory_bytes"] = memory
        if memory < report.get("estimated_memory_bytes", 0):
            warnings.append("memory_estimate_exceeds_available")
    report["ready"] = not errors
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--section-key", default="")
    parser.add_argument("--coords-key", default="spatial")
    parser.add_argument("--counts-layer", default="")
    parser.add_argument("--require-counts", action="store_true")
    parser.add_argument("--table", default="")
    parser.add_argument("--no-spatial", action="store_true")
    parser.add_argument("--spatial-x", default="")
    parser.add_argument("--spatial-y", default="")
    parser.add_argument("--coordinate-mode", choices=("strict", "export", "companion"), default="strict")
    args = parser.parse_args()
    report = check(args.input, args.output_dir, args.section_key, args.coords_key,
                   args.counts_layer, args.require_counts, args.table,
                   require_spatial=not args.no_spatial, spatial_x=args.spatial_x, spatial_y=args.spatial_y,
                   coordinate_mode=args.coordinate_mode)
    print("READINESS_JSON " + json.dumps(report))


if __name__ == "__main__":
    main()
