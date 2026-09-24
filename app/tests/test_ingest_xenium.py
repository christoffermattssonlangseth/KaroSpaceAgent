"""Local raw-Xenium ingestion: bundle discovery, argv wiring (no scientific stack
needed), tool registration, a real end-to-end assembly of synthetic Xenium output
bundles into one .h5ad, input rejection for already-assembled files, and — crucially
— that the privacy layer forwards only aggregate sample / cell / gene counts, never a
bundle folder name (which becomes a sample_id VALUE), a path, or a coordinate.
"""

import importlib.util
import json

import numpy as np
import pytest

from karospace_agent import commands


def load_ingest():
    spec = importlib.util.spec_from_file_location("ingest_xenium", commands.INGEST_XENIUM_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_bundle(bundle, n_cells=6, n_genes=4, n_controls=1):
    """Write a minimal Xenium output bundle: cell_feature_matrix.h5 + cells.csv.

    Mirrors the Xenium Onboard Analysis layout that geo_fetch._load_matrix reads:
    the matrix group stores a genes x cells CSC (transposed to cells x genes on
    load), with a features group carrying id / feature_type / name, plus barcodes.
    """
    import h5py
    import pandas as pd
    import scipy.sparse as sp

    bundle.mkdir(parents=True, exist_ok=True)
    genes_by_cells = np.ones((n_genes, n_cells), dtype=np.float32) * 3
    gxc = sp.csc_matrix(genes_by_cells)

    names = [f"Gene{i}" for i in range(n_genes - n_controls)]
    names += [f"NegControl{i}" for i in range(n_controls)]
    ftypes = ["Gene Expression"] * (n_genes - n_controls) + ["Negative Control Probe"] * n_controls
    barcodes = [f"cell{i}" for i in range(n_cells)]

    with h5py.File(bundle / "cell_feature_matrix.h5", "w") as f:
        g = f.create_group("matrix")
        g.create_dataset("data", data=gxc.data)
        g.create_dataset("indices", data=gxc.indices)
        g.create_dataset("indptr", data=gxc.indptr)
        g.create_dataset("shape", data=np.array([n_genes, n_cells], dtype=np.int64))
        g.create_dataset("barcodes", data=np.array([b.encode() for b in barcodes]))
        feat = g.create_group("features")
        feat.create_dataset("id", data=np.array([n.encode() for n in names]))
        feat.create_dataset("name", data=np.array([n.encode() for n in names]))
        feat.create_dataset("feature_type", data=np.array([t.encode() for t in ftypes]))

    pd.DataFrame({
        "cell_id": barcodes,
        "x_centroid": np.arange(n_cells, dtype=np.float32),
        "y_centroid": np.arange(n_cells, dtype=np.float32) * 2,
    }).to_csv(bundle / "cells.csv", index=False)
    return bundle


# --- Discoverability + argv wiring (no scientific stack needed) -------------

def test_ingest_script_is_discoverable():
    assert commands.INGEST_XENIUM_SCRIPT.exists()


def test_run_ingest_xenium_argv_carries_options(monkeypatch):
    captured = {}
    monkeypatch.setattr(commands, "run", lambda argv, timeout=0: captured.setdefault("argv", argv) or commands.RunResult(0, "", ""))
    monkeypatch.setattr(commands, "merge_python", lambda: "PY")
    commands.run_ingest_xenium(
        "bundles/", "out.h5ad", include_control=True,
        min_counts=40, min_genes=15, exclude=["skipme"],
    )
    argv = captured["argv"]
    assert argv[:3] == ["PY", str(commands.INGEST_XENIUM_SCRIPT), "bundles/"]
    assert "-o" in argv and "out.h5ad" in argv
    assert "--include-control" in argv
    assert "--min-counts" in argv and "40" in argv
    assert "--min-genes" in argv and "15" in argv
    assert "--exclude" in argv and "skipme" in argv


def test_run_ingest_xenium_omits_unset_options(monkeypatch):
    captured = {}
    monkeypatch.setattr(commands, "run", lambda argv, timeout=0: captured.setdefault("argv", argv) or commands.RunResult(0, "", ""))
    monkeypatch.setattr(commands, "merge_python", lambda: "PY")
    commands.run_ingest_xenium("bundles/", "out.h5ad")
    argv = captured["argv"]
    assert argv == ["PY", str(commands.INGEST_XENIUM_SCRIPT), "bundles/", "-o", "out.h5ad"]
    assert "--include-control" not in argv


def test_run_ingest_xenium_missing_script(monkeypatch):
    monkeypatch.setattr(commands, "INGEST_XENIUM_SCRIPT", commands.INGEST_XENIUM_SCRIPT.with_name("nope.py"))
    rr = commands.run_ingest_xenium("bundles/", "out.h5ad")
    assert rr.returncode == 127 and "ingest_xenium script missing" in rr.stderr


# --- Registration -----------------------------------------------------------

def test_ingest_xenium_is_a_registered_tool():
    from karospace_agent import agent, tools

    assert "ingest_xenium" in tools.TOOL_NAMES
    assert "mcp__karospace__ingest_xenium" in agent.ALLOWED_TOOL_NAMES


# --- Discovery + end-to-end (needs the scientific stack) ---------------------

def test_discover_bundles_finds_a_single_bundle_and_nested(tmp_path):
    pytest.importorskip("h5py")
    pytest.importorskip("scipy")
    mod = load_ingest()
    # A directory that *is* a bundle -> [itself].
    solo = write_bundle(tmp_path / "output-A")
    assert mod.discover_bundles(solo) == [solo]
    # A parent holding two bundles -> both, sorted, no descent into internals.
    root = tmp_path / "run"
    write_bundle(root / "output-A")
    write_bundle(root / "output-B")
    found = mod.discover_bundles(root)
    assert [p.name for p in found] == ["output-A", "output-B"]


def test_run_assembles_bundles_and_emits_aggregate_marker(tmp_path, capsys):
    pytest.importorskip("h5py")
    pytest.importorskip("anndata")
    pytest.importorskip("scipy")
    mod = load_ingest()
    root = tmp_path / "run"
    write_bundle(root / "output-A", n_cells=6, n_genes=4, n_controls=1)
    write_bundle(root / "output-B", n_cells=4, n_genes=4, n_controls=1)
    out = tmp_path / "assembled.h5ad"
    mod.run(root, out, include_control=False, min_counts=0, min_genes=0, exclude=[])

    assert out.exists() and out.stat().st_size > 0
    captured = capsys.readouterr().out
    line = next(l for l in captured.splitlines() if l.startswith(mod.JSON_MARKER))
    summary = json.loads(line[len(mod.JSON_MARKER):])
    assert summary["n_samples"] == 2
    assert summary["n_cells"] == 10
    assert summary["n_genes"] == 3          # the one control probe dropped
    assert summary["controls_dropped"] is True
    assert summary["sample_sizes"] == [6, 4]
    assert summary["qc"] is None
    # The bundle folder names became sample_id VALUES — they must not appear in the
    # aggregate marker the model would read.
    assert "output-A" not in line and "output-B" not in line


def test_run_honors_exclude_and_qc(tmp_path, capsys):
    pytest.importorskip("h5py")
    pytest.importorskip("anndata")
    pytest.importorskip("scipy")
    mod = load_ingest()
    root = tmp_path / "run"
    write_bundle(root / "output-keep", n_cells=6, n_genes=4)
    write_bundle(root / "output-drop", n_cells=4, n_genes=4)
    out = tmp_path / "assembled.h5ad"
    # Exclude one bundle; apply a QC threshold that keeps every remaining cell.
    mod.run(root, out, include_control=False, min_counts=1, min_genes=1, exclude=["drop"])

    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith(mod.JSON_MARKER))
    summary = json.loads(line[len(mod.JSON_MARKER):])
    assert summary["n_samples"] == 1 and summary["sample_sizes"] == [6]
    assert summary["qc"] == {"n_before": 6, "n_after": 6}


def test_nested_bundles_with_same_name_have_unique_local_ids(tmp_path, capsys):
    pytest.importorskip("h5py")
    ad = pytest.importorskip("anndata")
    pytest.importorskip("scipy")
    mod = load_ingest()
    root = tmp_path / "run"
    write_bundle(root / "sample-a" / "outs", n_cells=3)
    write_bundle(root / "sample-b" / "outs", n_cells=2)
    out = tmp_path / "assembled.h5ad"
    mod.run(root, out, False, 0, 0, [])

    assembled = ad.read_h5ad(out)
    assert assembled.n_obs == 5
    assert assembled.obs_names.is_unique
    assert assembled.obs["sample_id"].value_counts().to_dict() == {
        "sample-a/outs": 3, "sample-b/outs": 2,
    }
    assert assembled.obsm["spatial"].shape == (5, 2)
    line = next(l for l in capsys.readouterr().out.splitlines()
                if l.startswith(mod.JSON_MARKER))
    assert "sample-a" not in line and "sample-b" not in line and "outs" not in line
    assert json.loads(line[len(mod.JSON_MARKER):])["n_samples"] == 2


def test_single_bundle_keeps_its_folder_name(tmp_path):
    pytest.importorskip("h5py")
    ad = pytest.importorskip("anndata")
    pytest.importorskip("scipy")
    root = write_bundle(tmp_path / "output-single")
    out = tmp_path / "assembled.h5ad"
    load_ingest().run(root, out, False, 0, 0, [])
    assert set(ad.read_h5ad(out).obs["sample_id"]) == {"output-single"}


def test_already_assembled_input_is_rejected(tmp_path):
    mod = load_ingest()
    with pytest.raises(SystemExit, match="ingest_input_unsupported"):
        mod.run(tmp_path / "data.h5ad", tmp_path / "out.h5ad", False, 0, 0, [])


@pytest.mark.parametrize("destination", ["symlink", "existing"])
def test_ingest_preserves_existing_outputs(tmp_path, destination):
    pytest.importorskip("h5py")
    pytest.importorskip("anndata")
    pytest.importorskip("scipy")
    root = write_bundle(tmp_path / "run" / "output-A")
    out = tmp_path / "out.h5ad"
    if destination == "symlink":
        out.symlink_to(tmp_path / "run")
    else:
        out.write_bytes(b"existing output")
    previous_output = out.read_bytes() if destination == "existing" else None

    with pytest.raises(SystemExit, match="ingest_output_exists"):
        load_ingest().run(tmp_path / "run", out, False, 0, 0, [])
    if destination == "existing":
        assert out.read_bytes() == previous_output


def test_ingest_preserves_output_created_during_assembly(tmp_path, monkeypatch):
    pytest.importorskip("h5py")
    ad = pytest.importorskip("anndata")
    pytest.importorskip("scipy")
    write_bundle(tmp_path / "run" / "output-A")
    out = tmp_path / "out.h5ad"
    write = ad.AnnData.write_h5ad

    def concurrent_write(adata, *args, **kwargs):
        write(adata, *args, **kwargs)
        out.write_bytes(b"another process's output")

    monkeypatch.setattr(ad.AnnData, "write_h5ad", concurrent_write)
    with pytest.raises(SystemExit, match="ingest_output_exists"):
        load_ingest().run(tmp_path / "run", out, False, 0, 0, [])
    assert out.read_bytes() == b"another process's output"
    assert not list(tmp_path.glob(".ingest-*"))


def test_ingest_write_failure_leaves_no_partial_output(tmp_path, monkeypatch):
    pytest.importorskip("h5py")
    ad = pytest.importorskip("anndata")
    pytest.importorskip("scipy")
    write_bundle(tmp_path / "run" / "output-A")
    out = tmp_path / "out.h5ad"

    def failed_write(adata, filename, **kwargs):
        filename.write_bytes(b"partial file")
        raise OSError("synthetic disk error")

    monkeypatch.setattr(ad.AnnData, "write_h5ad", failed_write)
    with pytest.raises(OSError, match="synthetic disk error"):
        load_ingest().run(tmp_path / "run", out, False, 0, 0, [])
    assert not out.exists()
    assert not list(tmp_path.glob(".ingest-*"))


@pytest.mark.parametrize("diagnostic", ["ingest_output_exists", "ingest_no_bundles"])
def test_privacy_returns_fixed_ingest_diagnostics(diagnostic):
    from karospace_agent.privacy import Boundary

    raw = {"_local": {"returncode": 1, "stdout": "", "stderr":
                      f"{diagnostic}: /private/patients/subject-123 output-secret"}}
    res = Boundary(allow_local_paths=True).filter("ingest_xenium", {}, {}, raw)
    payload = res["content"][0]["text"]
    assert res["is_error"] is True
    assert json.loads(payload)["diagnostic"] == diagnostic
    assert "subject-123" not in payload and "output-secret" not in payload


def test_a_directory_with_no_bundle_is_rejected(tmp_path):
    pytest.importorskip("h5py")
    mod = load_ingest()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit, match="ingest_no_bundles"):
        mod.run(empty, tmp_path / "out.h5ad", False, 0, 0, [])


# --- Privacy branch (synthetic JSON, no scientific stack needed) ------------

def test_privacy_forwards_only_aggregate_counts_not_names_or_paths():
    from karospace_agent.privacy import Boundary

    boundary = Boundary(allow_local_paths=True)
    stdout = (
        "  assembled: 6 cells x 3 genes\n"
        "wrote: /private/patients/assembled.h5ad  (10 cells x 3 genes)\n"
        'INGEST_XENIUM_JSON {"n_samples": 2, "n_cells": 10, "n_genes": 3, '
        '"controls_dropped": true, "sample_sizes": [6, 4], "qc": null}'
    )
    raw = {"_local": {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}}
    res = boundary.filter("ingest_xenium", {"output": "/karo/out/assembled.h5ad"}, {}, raw)

    text = res["content"][0]["text"]
    data = json.loads(text)
    assert res["is_error"] is False
    assert data["n_samples"] == 2 and data["n_cells"] == 10 and data["n_genes"] == 3
    assert data["controls_dropped"] is True
    assert data["sample_sizes"] == [6, 4]
    # No bundle folder name, absolute local path, or coordinate crosses.
    assert "/private/patients" not in text
    assert "output-" not in text


def test_privacy_forwards_qc_counts_when_present():
    from karospace_agent.privacy import Boundary

    boundary = Boundary(allow_local_paths=True)
    # With QC, the post-filter n_cells can be < sum(sample_sizes) — the branch must
    # accept that rather than flagging an inconsistency.
    stdout = ('INGEST_XENIUM_JSON {"n_samples": 2, "n_cells": 7, "n_genes": 3, '
              '"controls_dropped": true, "sample_sizes": [6, 4], '
              '"qc": {"n_before": 10, "n_after": 7}}')
    raw = {"_local": {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}}
    res = boundary.filter("ingest_xenium", {}, {}, raw)
    data = json.loads(res["content"][0]["text"])
    assert res["is_error"] is False
    assert data["qc"] == {"n_before": 10, "n_after": 7}


def test_privacy_rejects_inconsistent_sample_sizes():
    from karospace_agent.privacy import Boundary

    boundary = Boundary(allow_local_paths=True)
    # sum(sample_sizes) != n_cells with no qc -> fail closed.
    stdout = ('INGEST_XENIUM_JSON {"n_samples": 2, "n_cells": 99, "n_genes": 3, '
              '"controls_dropped": true, "sample_sizes": [6, 4], "qc": null}')
    raw = {"_local": {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}}
    res = boundary.filter("ingest_xenium", {}, {}, raw)
    assert res["is_error"] is True
    assert json.loads(res["content"][0]["text"])["diagnostic"] == "schema_unavailable"


def test_privacy_rejects_a_missing_ingest_marker():
    from karospace_agent.privacy import Boundary

    boundary = Boundary(allow_local_paths=True)
    raw = {"_local": {"returncode": 0, "stdout": "no marker", "stderr": "", "timed_out": False}}
    res = boundary.filter("ingest_xenium", {}, {}, raw)
    assert res["is_error"] is True
    assert json.loads(res["content"][0]["text"])["diagnostic"] == "schema_unavailable"
