"""Check replicate provenance locally, without returning labels or data values."""
from __future__ import annotations

import argparse
from pathlib import Path


def check(root, replicate: str) -> str | None:
    if "obs" not in root or replicate not in root["obs"]:
        return "pseudobulk_replicate_required"
    if "uns" in root and "karospace_section_split" in root["uns"]:
        metadata = root["uns"]["karospace_section_split"]
        # Unrecognized provenance is not evidence of independent replicates.
        if "section_keys" not in metadata:
            return "pseudobulk_replicate_required"
        keys = metadata["section_keys"][()]
        keys = [v.decode() if isinstance(v, bytes) else str(v) for v in keys]
        if replicate in keys:
            return "pseudobulk_piece_replicate"
    return None


def validate(path: str, replicate: str, table: str = "") -> str | None:
    if Path(path).is_dir():
        import zarr
        root = zarr.open_group(path, mode="r")
        if "tables" in root:
            tables = root["tables"]
            if not table:
                names = list(tables.group_keys())
                if len(names) != 1:
                    return "pseudobulk_replicate_required"
                table = names[0]
            root = tables[table]
        return check(root, replicate)
    import h5py
    with h5py.File(path, "r") as root:
        return check(root, replicate)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--replicate", required=True)
    parser.add_argument("--table", default="")
    args = parser.parse_args()
    try:
        diagnostic = validate(args.input, args.replicate, args.table)
    except Exception:
        diagnostic = "pseudobulk_replicate_required"
    if diagnostic:
        print(diagnostic)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
