#!/usr/bin/env python
"""Fetch a GEO accession and assemble the *minimal* AnnData needed to drive a
KaroSpace viewer — downloading only the members that build the cell x gene
matrix, never the whole multi-gigabyte supplementary bundle.

Two subcommands, mirroring the local-hands / schema-only split the rest of this
repo uses:

    manifest <GSE|GSM>                 # list samples, files, sizes, platform
    build <GSE|GSM> --gsm ... --platform xenium -o out.h5ad

Why selective download
----------------------
A GEO Xenium sample ships one `*_outs.zip` of ~8-10 GB, but the AnnData only
needs `cell_feature_matrix.h5` (X + var) and `cells.parquet` (obs + centroids)
— a few hundred MB. Those two are pulled out of the remote zip with HTTP range
requests (`remotezip`, which understands ZIP64), so the transcripts table and
the morphology OME-TIFFs — the bulk of the archive — are never transferred.
The 31 GB series `RAW.tar` is likewise avoided: every file is also mirrored
per-sample under geo/samples/GSMnnnNNN/GSMXXXXX/suppl/.

Data-handling boundary
----------------------
`manifest` emits only PUBLIC GEO catalogue facts (study title/summary, sample
titles, organism, instrument, supplementary FILENAMES + sizes, inferred
platform) — the metadata a caller needs to choose which samples to build.
`build` runs entirely locally and prints only an AGGREGATE log: which members
were downloaded and their sizes, then cell/gene counts, obs/var column NAMES,
and obsm KEYS of the result. No expression values, coordinates, or per-cell
identifiers are printed — inspect the written .h5ad with inspect_input /
inspect_structure for the schema, exactly as for any other input.

The heavy read + assembly stays on this machine; only the facts above cross.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

GEO_ACC_URL = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"
FTP_HOST = "https://ftp.ncbi.nlm.nih.gov"
HTTP_TIMEOUT = 60


# --- GEO catalogue queries (public metadata only) --------------------------

def _acc_text(accession: str) -> dict[str, list[str]]:
    """Fetch a GEO record in `form=text` and group its `!Key = value` lines.

    Works for both series (GSE) and samples (GSM); the prefix differs
    (!Series_ / !Sample_) but the shape is identical. Returns key -> [values].
    """
    q = urllib.parse.urlencode(
        {"acc": accession, "targ": "self", "form": "text", "view": "quick"}
    )
    url = f"{GEO_ACC_URL}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "karospace-agent/geo"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    grouped: dict[str, list[str]] = {}
    for line in body.splitlines():
        if not line.startswith("!"):
            continue
        if " = " not in line:
            continue
        key, _, val = line[1:].partition(" = ")
        grouped.setdefault(key.strip(), []).append(val.strip())
    return grouped


def _first(grouped: dict[str, list[str]], key: str, default: str = "") -> str:
    vals = grouped.get(key)
    return vals[0] if vals else default


def _ftp_to_https(url: str) -> str:
    """GEO records give ftp:// URLs; the same host serves https with range support."""
    if url.startswith("ftp://"):
        return "https://" + url[len("ftp://"):]
    return url


def _remote_size(url: str) -> int | None:
    """HEAD the file for its Content-Length (bytes). None if unavailable."""
    try:
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "karospace-agent/geo"}
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            cl = resp.headers.get("Content-Length")
            return int(cl) if cl else None
    except Exception:
        return None


def _sample_files(gsm_record: dict[str, list[str]]) -> list[str]:
    """All supplementary file URLs (https, range-capable) for a GSM record."""
    urls: list[str] = []
    for key, vals in gsm_record.items():
        if key.startswith("Sample_supplementary_file"):
            urls.extend(_ftp_to_https(v) for v in vals if v and v != "NONE")
    return urls


# --- Platform inference (from public filenames + instrument) ---------------

def infer_platform(filenames: list[str], instrument: str) -> str:
    joined = " ".join(filenames).lower() + " " + instrument.lower()
    if "xenium" in joined or "_outs.zip" in joined:
        return "xenium"
    if "cell_by_gene" in joined or "cell_metadata" in joined or "merscope" in joined or "merfish" in joined or "vizgen" in joined:
        return "merscope"
    if "binned_outputs" in joined or "square_016um" in joined or "visium_hd" in joined:
        return "visium_hd"
    if "tissue_positions" in joined or ("spatial" in joined and "filtered_feature_bc_matrix" in joined):
        return "visium"
    if "cosmx" in joined or "exprmat" in joined:
        return "cosmx"
    if "geomx" in joined or "dcc" in joined:
        return "geomx"
    if "filtered_feature_bc_matrix" in joined:
        return "chromium"
    return "unknown"


def _human(n: int | None) -> str:
    if n is None:
        return "?"
    step = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if step < 1024 or unit == "TB":
            return f"{step:.1f} {unit}" if unit != "B" else f"{int(step)} B"
        step /= 1024
    return f"{n} B"


# --- manifest --------------------------------------------------------------

def build_manifest(accession: str, sizes: bool = True) -> dict:
    rec = _acc_text(accession)
    is_series = accession.upper().startswith("GSE")

    if is_series:
        gsm_ids = rec.get("Series_sample_id", [])
        summary = _first(rec, "Series_summary")
        out: dict = {
            "accession": accession,
            "title": _first(rec, "Series_title"),
            "summary": summary[:400] + ("..." if len(summary) > 400 else ""),
            "n_samples": len(gsm_ids),
            "samples": [],
        }
        for gsm in gsm_ids:
            out["samples"].append(_manifest_sample(gsm, sizes))
        return out

    return {"accession": accession, "title": "", "summary": "",
            "n_samples": 1, "samples": [_manifest_sample(accession, sizes)]}


def _manifest_sample(gsm: str, sizes: bool) -> dict:
    rec = _acc_text(gsm)
    urls = _sample_files(rec)
    files = []
    for url in urls:
        name = url.rsplit("/", 1)[-1]
        files.append({
            "name": name,
            "url": url,
            "size_bytes": _remote_size(url) if sizes else None,
        })
    instrument = _first(rec, "Sample_instrument_model")
    platform = infer_platform([f["name"] for f in files], instrument)
    return {
        "gsm": gsm,
        "title": _first(rec, "Sample_title"),
        "organism": _first(rec, "Sample_organism_ch1"),
        "instrument": instrument,
        "platform": platform,
        "files": files,
    }


# Basenames each platform's assembler consumes (what `build` will actually pull).
BUILD_NEEDS = {
    "xenium": ["cell_feature_matrix.h5", "cells.parquet | cells.csv.gz"],
    "visium": ["filtered_feature_bc_matrix.h5", "tissue_positions (.csv/.parquet)"],
    "merscope": ["cell_by_gene.csv[.gz]", "cell_metadata.csv[.gz]"],
    "visium_hd": ["binned_outputs/square_*/filtered_feature_bc_matrix.h5", "tissue_positions.parquet"],
    "chromium": ["filtered_feature_bc_matrix.h5"],
}


def format_manifest(m: dict) -> str:
    lines = [f"GEO manifest: {m['accession']}"]
    if m.get("title"):
        lines.append(f"title: {m['title']}")
    if m.get("summary"):
        lines.append(f"summary: {m['summary']}")
    lines.append(f"samples: {m['n_samples']}")
    for s in m["samples"]:
        lines.append("")
        lines.append(f"{s['gsm']}  [{s['platform']}]  {s['title']}")
        if s.get("organism"):
            lines.append(f"  organism: {s['organism']}")
        if s.get("instrument"):
            lines.append(f"  instrument: {s['instrument']}")
        lines.append("  files:")
        for f in s["files"]:
            lines.append(f"    - {f['name']}  ({_human(f['size_bytes'])})")
        needs = BUILD_NEEDS.get(s["platform"])
        if needs:
            lines.append("  build pulls only: " + ", ".join(needs))
        else:
            lines.append("  build: platform not yet supported by geo_build")
    return "\n".join(lines)


# --- selective download ----------------------------------------------------

def _pull_zip_members(url: str, wanted: list[str], dest: Path, log: list[str]) -> dict[str, Path]:
    """Extract only `wanted` basenames from a remote zip via range requests.

    Returns basename -> local path for those found. Members are matched by
    basename so a nested `outs/cell_feature_matrix.h5` still resolves.
    """
    from remotezip import RemoteZip  # lazy: only needed for zip-bundle platforms

    dest.mkdir(parents=True, exist_ok=True)
    found: dict[str, Path] = {}
    # Reuse anything already pulled on a previous run — the range download is the
    # slow part, so a warm cache turns a rebuild into a local read.
    remaining = []
    for want in wanted:
        cached = dest / want
        if cached.exists():
            found[want] = cached
            log.append(f"    = {want}  (cached)")
        else:
            remaining.append(want)
    if not remaining:
        return found
    log.append(f"  open (range): {url.rsplit('/', 1)[-1]}")
    with RemoteZip(url) as zf:
        names = zf.namelist()
        for want in remaining:
            match = next((n for n in names if n.rsplit("/", 1)[-1] == want), None)
            if match is None:
                continue
            info = zf.getinfo(match)
            target = dest / want
            with zf.open(match) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            found[want] = target
            log.append(f"    + {want}  ({_human(info.compress_size)} transferred)")
    return found


def _download(url: str, dest: Path, log: list[str]) -> Path:
    """Stream a whole supplementary file to disk (small companions: parquet, json)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "karospace-agent/geo"})
    total = 0
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            total += len(chunk)
    log.append(f"    + {dest.name}  ({_human(total)} transferred)")
    return dest


def _find_file(sample: dict, *substrings: str) -> dict | None:
    """First supplementary file whose name contains every substring (lowercased)."""
    for f in sample["files"]:
        low = f["name"].lower()
        if all(s.lower() in low for s in substrings):
            return f
    return None


def _fetch_file(sample: dict, cache: Path, log: list[str], *substrings: str,
                role: str) -> Path:
    """Download a role's loose supplementary file, gunzipping a `.gz` payload.

    Fails loudly, listing the sample's actual filenames, when the expected file
    is not among them — that is the signal the GEO layout is not the canonical
    one this assembler handles (e.g. everything is inside a tar), rather than a
    silent wrong guess.
    """
    hit = _find_file(sample, *substrings)
    if hit is None:
        names = ", ".join(f["name"] for f in sample["files"]) or "(none)"
        raise ValueError(
            f"{sample['gsm']}: no supplementary file matching {'+'.join(substrings)} "
            f"for '{role}'. Files present: {names}"
        )
    dest = cache / sample["gsm"] / hit["name"]
    if not dest.exists():
        _download(hit["url"], dest, log)
    else:
        log.append(f"    = {hit['name']}  (cached)")
    return _gunzip_if_needed(dest)


def _gunzip_if_needed(path: Path) -> Path:
    """Decompress a `.gz` file to its stem (once); pass anything else through.

    Left as `.gz` are formats pandas/h5py read compressed directly (`.csv.gz`);
    this only bites where a decompressed file on disk is required.
    """
    if path.suffix != ".gz":
        return path
    # csv.gz / tsv.gz are read compressed by pandas — no need to expand them.
    if path.suffixes[-2:-1] and path.suffixes[-2] in (".csv", ".tsv"):
        return path
    import gzip
    import shutil

    out = path.with_suffix("")
    if not out.exists():
        with gzip.open(path, "rb") as fin, open(out, "wb") as fout:
            shutil.copyfileobj(fin, fout)
    return out


# --- xenium assembler ------------------------------------------------------

def _load_matrix(matrix_h5: Path):
    """cell_feature_matrix.h5 -> (X csr float32 cells x genes, var, barcodes)."""
    import h5py
    import numpy as np
    import pandas as pd
    from scipy import sparse

    with h5py.File(matrix_h5, "r") as f:
        g = f["matrix"]
        X = sparse.csc_matrix(
            (g["data"][:], g["indices"][:], g["indptr"][:]),
            shape=tuple(int(x) for x in g["shape"][:]),
        ).T.tocsr().astype(np.float32)
        feat = g["features"]
        var = pd.DataFrame(
            {
                "feature_id": [x.decode() for x in feat["id"][:]],
                "feature_type": [x.decode() for x in feat["feature_type"][:]],
            },
            index=[x.decode() for x in feat["name"][:]],
        )
        var.index.name = "gene"
        barcodes = pd.Index([x.decode() for x in g["barcodes"][:]], name="cell_id")
    return X, var, barcodes


def _read_cells(cells_path: Path):
    import pandas as pd

    df = pd.read_parquet(cells_path) if cells_path.suffix == ".parquet" else pd.read_csv(cells_path)
    df["cell_id"] = df["cell_id"].astype(str)
    return df


def _sanitize_obs(obs_df):
    import pandas as pd

    out = obs_df.copy()
    for col in out.columns:
        ser = out[col]
        if isinstance(ser.dtype, pd.CategoricalDtype):
            continue
        if pd.api.types.is_object_dtype(ser) or pd.api.types.is_string_dtype(ser):
            out[col] = ser.astype("string")
    return out


def _assemble_xenium_sample(gsm: str, url: str, cache: Path, include_control: bool, log: list[str]):
    """Pull the two matrix members from a GSM's outs.zip and build one AnnData."""
    import anndata as ad
    import numpy as np

    sample_cache = cache / gsm
    log.append(f"{gsm}: xenium")
    members = _pull_zip_members(
        url, ["cell_feature_matrix.h5", "cells.parquet", "cells.csv.gz"], sample_cache, log
    )
    if "cell_feature_matrix.h5" not in members:
        raise ValueError(f"{gsm}: cell_feature_matrix.h5 not found in {url.rsplit('/', 1)[-1]}")
    cells_path = members.get("cells.parquet") or members.get("cells.csv.gz")
    if cells_path is None:
        raise ValueError(f"{gsm}: neither cells.parquet nor cells.csv.gz found")

    X, var, barcodes = _load_matrix(members["cell_feature_matrix.h5"])
    cells = _read_cells(cells_path).set_index("cell_id").reindex(barcodes)
    obs = _finish_obs(cells, gsm)

    if not include_control:
        keep = (var["feature_type"] == "Gene Expression").to_numpy()
        X, var = X[:, keep], var.loc[keep]

    a = ad.AnnData(X=X, obs=obs, var=var)
    if {"x_centroid", "y_centroid"}.issubset(obs.columns):
        a.obsm["spatial"] = obs[["x_centroid", "y_centroid"]].to_numpy(np.float32)
    log.append(f"  assembled: {a.n_obs} cells x {a.n_vars} genes")
    return a


def _finish_obs(obs, gsm: str):
    """Common obs finishing: sample_id first, gsm-prefixed index, h5ad-safe types."""
    obs = obs.copy()
    obs.insert(0, "sample_id", gsm)
    obs.index = gsm + "__" + obs.index.astype(str)
    obs.index.name = "cell_id"
    return _sanitize_obs(obs)


def _concat(adatas: list, samples: list[dict], log: list[str]):
    import anndata as ad

    if len(adatas) == 1:
        return adatas[0]
    merged = ad.concat(adatas, axis=0, join="outer", merge="same",
                       keys=[s["gsm"] for s in samples], index_unique=None)
    log.append(f"  merged {len(adatas)} samples: {merged.n_obs} cells x {merged.n_vars} genes")
    return merged


def _xenium_outs_url(sample: dict) -> str | None:
    for f in sample["files"]:
        if f["name"].endswith("_outs.zip") or f["name"] == "outs.zip":
            return f["url"]
    return None


def build_xenium(samples: list[dict], cache: Path, include_control: bool, log: list[str]):
    adatas = [
        _assemble_xenium_sample(s["gsm"], _require_outs(s), cache, include_control, log)
        for s in samples
    ]
    return _concat(adatas, samples, log)


def _require_outs(sample: dict) -> str:
    url = _xenium_outs_url(sample)
    if url is None:
        names = ", ".join(f["name"] for f in sample["files"]) or "(none)"
        raise ValueError(
            f"{sample['gsm']}: no *_outs.zip on the GEO record. Files present: {names}"
        )
    return url


# --- visium assembler ------------------------------------------------------

def _read_positions(path: Path):
    """10x tissue_positions -> DataFrame indexed by spot barcode.

    Handles both the newer headered CSV/parquet and the older headerless CSV
    (barcode,in_tissue,array_row,array_col,pxl_row_in_fullres,pxl_col_in_fullres).
    """
    import pandas as pd

    cols = ["barcode", "in_tissue", "array_row", "array_col",
            "pxl_row_in_fullres", "pxl_col_in_fullres"]
    if path.suffix == ".parquet" or ".parquet" in path.suffixes:
        df = pd.read_parquet(path)
    else:
        head = pd.read_csv(path, nrows=1, header=None)
        headered = str(head.iloc[0, 0]).lower() in ("barcode", "barcodes")
        df = pd.read_csv(path, header=0 if headered else None)
        if not headered:
            df = df.iloc[:, :6]
            df.columns = cols
    df = df.rename(columns={c: c.lower() for c in df.columns})
    if "barcode" not in df.columns:
        df = df.rename(columns={df.columns[0]: "barcode"})
    return df.set_index("barcode")


def _assemble_visium_sample(sample: dict, cache: Path, include_control: bool, log: list[str]):
    import anndata as ad
    import numpy as np

    gsm = sample["gsm"]
    log.append(f"{gsm}: visium")
    matrix = _fetch_file(sample, cache, log, "filtered_feature_bc_matrix.h5", role="matrix")
    positions = _fetch_file(sample, cache, log, "tissue_positions", role="positions")

    X, var, barcodes = _load_matrix(matrix)
    pos = _read_positions(positions).reindex(barcodes)
    obs = _finish_obs(pos, gsm)

    if not include_control and "feature_type" in var:
        keep = (var["feature_type"] == "Gene Expression").to_numpy()
        if keep.any():
            X, var = X[:, keep], var.loc[keep]

    a = ad.AnnData(X=X, obs=obs, var=var)
    a.var_names_make_unique()  # Visium symbols repeat (multiple Ensembl -> one symbol)
    if {"pxl_col_in_fullres", "pxl_row_in_fullres"}.issubset(obs.columns):
        a.obsm["spatial"] = obs[["pxl_col_in_fullres", "pxl_row_in_fullres"]].to_numpy(np.float32)
    log.append(f"  assembled: {a.n_obs} spots x {a.n_vars} genes")
    return a


def build_visium(samples: list[dict], cache: Path, include_control: bool, log: list[str]):
    adatas = [_assemble_visium_sample(s, cache, include_control, log) for s in samples]
    return _concat(adatas, samples, log)


# --- merscope / merfish assembler (Vizgen) ---------------------------------

def _assemble_merscope_sample(sample: dict, cache: Path, include_control: bool, log: list[str]):
    """Canonical Vizgen output: cell_by_gene.csv (counts) + cell_metadata.csv."""
    import anndata as ad
    import numpy as np
    import pandas as pd
    from scipy import sparse

    gsm = sample["gsm"]
    log.append(f"{gsm}: merscope")
    cbg_path = _fetch_file(sample, cache, log, "cell_by_gene", role="counts")
    meta_path = _fetch_file(sample, cache, log, "cell_metadata", role="metadata")

    cbg = pd.read_csv(cbg_path, index_col=0)
    cbg.index = cbg.index.astype(str)
    # Vizgen names negative-control features Blank-* / NoTarget-*; flag them so
    # they can be dropped like Xenium control probes. (Index.str.* -> ndarray.)
    low = cbg.columns.str.lower()
    blank = np.asarray(low.str.startswith("blank") | low.str.startswith("notarget"))
    var = pd.DataFrame(index=pd.Index(cbg.columns, name="gene"))
    var["feature_type"] = np.where(blank, "Blank", "Gene Expression")
    X = sparse.csr_matrix(cbg.to_numpy(dtype=np.float32))

    meta = pd.read_csv(meta_path, index_col=0)
    meta.index = meta.index.astype(str)
    obs = _finish_obs(meta.reindex(cbg.index), gsm)

    if not include_control:
        keep = ~blank
        X, var = X[:, keep], var.loc[keep]

    a = ad.AnnData(X=X, obs=obs, var=var)
    if {"center_x", "center_y"}.issubset(obs.columns):
        a.obsm["spatial"] = obs[["center_x", "center_y"]].to_numpy(np.float32)
    log.append(f"  assembled: {a.n_obs} cells x {a.n_vars} genes")
    return a


def build_merscope(samples: list[dict], cache: Path, include_control: bool, log: list[str]):
    adatas = [_assemble_merscope_sample(s, cache, include_control, log) for s in samples]
    return _concat(adatas, samples, log)


ASSEMBLERS = {
    "xenium": build_xenium,
    "visium": build_visium,
    "merscope": build_merscope,
}


# --- build -----------------------------------------------------------------

def run_build(accession: str, gsm_ids: list[str], platform: str, output: Path,
              cache: Path, include_control: bool) -> str:
    log: list[str] = [f"GEO build: {accession}  platform={platform}"]

    manifest = build_manifest(accession, sizes=False)
    by_gsm = {s["gsm"]: s for s in manifest["samples"]}

    if not gsm_ids:
        gsm_ids = [s["gsm"] for s in manifest["samples"] if s["platform"] == platform]
        log.append(f"selected all {platform} samples: {', '.join(gsm_ids) or '(none)'}")
    missing = [g for g in gsm_ids if g not in by_gsm]
    if missing:
        raise ValueError(f"GSM(s) not in {accession}: {', '.join(missing)}")
    if not gsm_ids:
        raise ValueError(f"no {platform} samples found in {accession}")

    selected = [by_gsm[g] for g in gsm_ids]

    assembler = ASSEMBLERS.get(platform)
    if assembler is None:
        needs = BUILD_NEEDS.get(platform)
        raise ValueError(
            f"platform '{platform}' not yet implemented by geo_build. "
            + (f"An assembler would need: {', '.join(needs)}." if needs
               else "No recipe is registered for it.")
        )

    adata = assembler(selected, cache, include_control, log)

    output.parent.mkdir(parents=True, exist_ok=True)
    # _sanitize_obs stores text columns as pandas nullable "string"; anndata >=0.11
    # gates writing those behind an opt-in setting. Enable it for this write.
    try:
        import anndata

        if hasattr(anndata, "settings"):
            anndata.settings.allow_write_nullable_strings = True
    except Exception:
        pass
    adata.write_h5ad(output, compression="gzip")

    log.append("")
    log.append(f"wrote: {output}  ({adata.n_obs} cells x {adata.n_vars} genes)")
    log.append("obs columns: " + ", ".join(map(str, adata.obs.columns)))
    log.append("var columns: " + ", ".join(map(str, adata.var.columns)))
    log.append("obsm keys: " + (", ".join(adata.obsm.keys()) or "(none)"))
    log.append("Next: inspect_input / inspect_structure on the written file for the schema.")
    return "\n".join(log)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch a GEO accession into a minimal AnnData.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("manifest", help="List samples, files, sizes, inferred platform.")
    m.add_argument("accession", help="GSE or GSM accession.")
    m.add_argument("--no-sizes", action="store_true", help="Skip HEAD requests for file sizes.")

    b = sub.add_parser("build", help="Download only the matrix members and assemble a .h5ad.")
    b.add_argument("accession", help="GSE or GSM accession.")
    b.add_argument("--gsm", action="append", default=[], help="Sample to include (repeatable). Default: all of --platform.")
    b.add_argument("--platform", default="xenium", help="Assembler to use (xenium supported).")
    b.add_argument("-o", "--output", required=True, help="Output .h5ad path.")
    b.add_argument("--cache-dir", default="", help="Where to stash pulled members. Default: alongside output.")
    b.add_argument("--include-control-features", action="store_true",
                   help="Keep negative-control / blank probes (default: Gene Expression only).")

    args = ap.parse_args()

    try:
        if args.cmd == "manifest":
            print(format_manifest(build_manifest(args.accession, sizes=not args.no_sizes)))
            return 0
        output = Path(args.output)
        cache = Path(args.cache_dir) if args.cache_dir else output.parent / f"{args.accession}_cache"
        print(run_build(args.accession, args.gsm, args.platform, output, cache,
                        args.include_control_features))
        return 0
    except Exception as e:  # noqa: BLE001 - report, don't crash the agent
        print(f"geo_fetch {args.cmd} failed for {args.accession}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
