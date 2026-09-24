"""The standalone QC filter: the filter_cells core, argv wiring (no scanpy needed),
tool registration, a real end-to-end filter on a synthetic .h5ad, and — crucially
— that the privacy layer forwards only aggregate before/after cell counts, never a
per-cell total, and fails closed on a malformed or inconsistent summary.
"""

import importlib.util
import json

import numpy as np
import pytest

from karospace_agent import commands


def load_qc():
    spec = importlib.util.spec_from_file_location("qc_filter", commands.QC_FILTER_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tiny_adata(n_cells=10, n_genes=6):
    """A cells x genes AnnData whose first two cells are near-empty (QC should drop)."""
    import anndata as ad
    import scipy.sparse as sp

    X = np.ones((n_cells, n_genes), dtype=np.float32) * 5
    X[0] = 0            # zero counts, zero genes -> dropped by any threshold
    X[1, 1:] = 0        # one gene, low counts
    adata = ad.AnnData(X=sp.csr_matrix(X))
    adata.obs["sample_id"] = "S1"
    return adata


# --- Discoverability + argv wiring (no scientific stack needed) -------------

def test_qc_script_is_discoverable():
    assert commands.QC_FILTER_SCRIPT.exists()


def test_run_qc_filter_argv_carries_thresholds(monkeypatch):
    captured = {}
    monkeypatch.setattr(commands, "run", lambda argv, timeout=0: captured.setdefault("argv", argv) or commands.RunResult(0, "", ""))
    monkeypatch.setattr(commands, "merge_python", lambda: "PY")
    commands.run_qc_filter("in.h5ad", "out.h5ad", min_counts=40, min_genes=15)
    argv = captured["argv"]
    assert argv[:3] == ["PY", str(commands.QC_FILTER_SCRIPT), "in.h5ad"]
    assert "-o" in argv and "out.h5ad" in argv
    assert "--min-counts" in argv and "40" in argv
    assert "--min-genes" in argv and "15" in argv


def test_run_qc_filter_omits_unset_thresholds(monkeypatch):
    captured = {}
    monkeypatch.setattr(commands, "run", lambda argv, timeout=0: captured.setdefault("argv", argv) or commands.RunResult(0, "", ""))
    monkeypatch.setattr(commands, "merge_python", lambda: "PY")
    commands.run_qc_filter("in.h5ad", "out.h5ad")
    argv = captured["argv"]
    assert argv == ["PY", str(commands.QC_FILTER_SCRIPT), "in.h5ad", "-o", "out.h5ad"]


def test_run_qc_filter_missing_script(monkeypatch):
    monkeypatch.setattr(commands, "QC_FILTER_SCRIPT", commands.QC_FILTER_SCRIPT.with_name("nope.py"))
    rr = commands.run_qc_filter("in.h5ad", "out.h5ad", min_counts=40)
    assert rr.returncode == 127 and "qc_filter script missing" in rr.stderr


# --- Registration -----------------------------------------------------------

def test_qc_filter_is_a_registered_tool():
    from karospace_agent import agent, tools

    assert "qc_filter" in tools.TOOL_NAMES
    assert "mcp__karospace__qc_filter" in agent.ALLOWED_TOOL_NAMES


# --- filter_cells core + end-to-end (needs the scientific stack) ------------

def test_filter_cells_threshold_zero_is_inactive():
    pytest.importorskip("scipy")
    pytest.importorskip("anndata")
    mod = load_qc()
    filtered, n_before, n_after = mod.filter_cells(tiny_adata(), 0, 0)
    assert n_before == n_after == 10


def test_filter_cells_drops_below_either_threshold():
    pytest.importorskip("scipy")
    pytest.importorskip("anndata")
    mod = load_qc()
    # min_counts=1 drops the all-zero cell; min_genes=2 drops the single-gene cell too.
    _, n_before, n_after = mod.filter_cells(tiny_adata(), min_counts=1, min_genes=2)
    assert n_before == 10 and n_after == 8


def test_run_writes_a_filtered_h5ad_and_emits_marker(tmp_path, capsys):
    pytest.importorskip("scipy")
    pytest.importorskip("anndata")
    mod = load_qc()
    src = tmp_path / "in.h5ad"
    tiny_adata().write_h5ad(src)
    out = tmp_path / "filtered.h5ad"
    mod.run(src, out, min_counts=1, min_genes=2)

    assert out.exists() and out.stat().st_size > 0
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith(mod.JSON_MARKER))
    summary = json.loads(line[len(mod.JSON_MARKER):])
    assert summary["n_before"] == 10 and summary["n_after"] == 8
    assert summary["n_removed"] == 2


def test_non_h5ad_input_is_rejected(tmp_path):
    mod = load_qc()
    with pytest.raises(SystemExit, match="qc_input_unsupported"):
        mod.run(tmp_path / "data.zarr", tmp_path / "out.h5ad", 40, 0)


def test_no_threshold_is_rejected(tmp_path):
    mod = load_qc()
    with pytest.raises(SystemExit, match="qc_no_threshold"):
        mod.run(tmp_path / "in.h5ad", tmp_path / "out.h5ad", 0, 0)


@pytest.mark.parametrize("destination", ["source", "symlink", "hardlink", "existing"])
def test_qc_preserves_input_and_existing_outputs(tmp_path, destination):
    pytest.importorskip("anndata")
    pytest.importorskip("scipy")
    src = tmp_path / "in.h5ad"
    tiny_adata().write_h5ad(src)
    original = src.read_bytes()
    out = tmp_path / "out.h5ad"
    if destination == "source":
        out = src
    elif destination == "symlink":
        out.symlink_to(src)
    elif destination == "hardlink":
        out.hardlink_to(src)
    else:
        out.write_bytes(b"existing output")
    previous_output = out.read_bytes()

    with pytest.raises(SystemExit, match="qc_output_"):
        load_qc().run(src, out, 1, 2)
    assert src.read_bytes() == original
    assert out.read_bytes() == previous_output


def test_qc_preserves_output_created_during_filtering(tmp_path, monkeypatch):
    ad = pytest.importorskip("anndata")
    pytest.importorskip("scipy")
    src, out = tmp_path / "in.h5ad", tmp_path / "out.h5ad"
    tiny_adata().write_h5ad(src)
    write = ad.AnnData.write_h5ad

    def concurrent_write(adata, *args, **kwargs):
        write(adata, *args, **kwargs)
        out.write_bytes(b"another process's output")

    monkeypatch.setattr(ad.AnnData, "write_h5ad", concurrent_write)
    with pytest.raises(SystemExit, match="qc_output_exists"):
        load_qc().run(src, out, 1, 2)
    assert out.read_bytes() == b"another process's output"
    assert not list(tmp_path.glob(".qc-*"))


def test_qc_write_failure_leaves_no_partial_output(tmp_path, monkeypatch):
    ad = pytest.importorskip("anndata")
    pytest.importorskip("scipy")
    src, out = tmp_path / "in.h5ad", tmp_path / "out.h5ad"
    tiny_adata().write_h5ad(src)
    original = src.read_bytes()

    def failed_write(adata, filename, **kwargs):
        filename.write_bytes(b"partial file")
        raise OSError("synthetic disk error")

    monkeypatch.setattr(ad.AnnData, "write_h5ad", failed_write)
    with pytest.raises(OSError, match="synthetic disk error"):
        load_qc().run(src, out, 1, 2)
    assert src.read_bytes() == original
    assert not out.exists()
    assert not list(tmp_path.glob(".qc-*"))


@pytest.mark.parametrize("storage", ["dense", "csr", "csc"])
@pytest.mark.parametrize("invalid", [0.5, -1, np.nan, np.inf])
def test_qc_rejects_invalid_counts_without_writing(tmp_path, storage, invalid):
    ad = pytest.importorskip("anndata")
    sp = pytest.importorskip("scipy.sparse")
    matrix = np.array([[invalid, 1], [2, 3]], dtype=np.float64)
    if storage != "dense":
        matrix = getattr(sp, storage + "_matrix")(matrix)
    adata = ad.AnnData(matrix)
    # A counts layer must not silently override the documented X input.
    adata.layers["counts"] = np.ones((2, 2), dtype=np.float64)
    src, out = tmp_path / "in.h5ad", tmp_path / "out.h5ad"
    adata.write_h5ad(src)
    with pytest.raises(SystemExit, match="qc_counts_required"):
        load_qc().run(src, out, 1, 0)
    assert not out.exists()


@pytest.mark.parametrize("storage", ["dense", "csr", "csc"])
def test_qc_accepts_integer_valued_floats_and_preserves_alignment(storage):
    ad = pytest.importorskip("anndata")
    sp = pytest.importorskip("scipy.sparse")
    matrix = np.array([[0, 0], [1, 0], [2, 3]], dtype=np.float32)
    if storage != "dense":
        matrix = getattr(sp, storage + "_matrix")(matrix)
    adata = ad.AnnData(matrix)
    adata.obsm["spatial"] = np.array([[0, 1], [2, 3], [4, 5]])
    adata.layers["counts"] = matrix.copy()
    filtered, before, after = load_qc().filter_cells(adata, 1, 2)
    assert (before, after) == (3, 1)
    assert filtered.obs_names.tolist() == ["2"]
    np.testing.assert_array_equal(filtered.obsm["spatial"], [[4, 5]])
    assert filtered.layers["counts"].shape == (1, 2)
    assert adata.n_obs == 3


@pytest.mark.parametrize("diagnostic", [
    "qc_counts_required", "qc_output_same_as_input", "qc_output_exists",
])
def test_privacy_returns_fixed_qc_diagnostics(diagnostic):
    from karospace_agent.privacy import Boundary

    raw = {"_local": {"returncode": 1, "stdout": "", "stderr":
                      f"{diagnostic}: /private/patients/subject-123.h5ad value=123.45"}}
    res = Boundary(allow_local_paths=True).filter("qc_filter", {}, {}, raw)
    payload = res["content"][0]["text"]
    assert res["is_error"] is True
    assert json.loads(payload)["diagnostic"] == diagnostic
    assert "subject-123" not in payload and "123.45" not in payload


# --- Privacy branch (synthetic JSON, no scientific stack needed) ------------

def test_privacy_forwards_only_aggregate_cell_counts():
    from karospace_agent.privacy import Boundary

    boundary = Boundary(allow_local_paths=True)
    stdout = (
        "filtered: 10 -> 8 cells\n"
        "wrote: /private/patients/filtered.h5ad\n"
        'QC_FILTER_JSON {"n_before": 10, "n_after": 8, "n_removed": 2, '
        '"min_counts": 40, "min_genes": 15}'
    )
    raw = {"_local": {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}}
    res = boundary.filter("qc_filter", {"output": "/karo/out/filtered.h5ad"}, {}, raw)

    text = res["content"][0]["text"]
    data = json.loads(text)
    assert res["is_error"] is False
    assert data["n_before"] == 10 and data["n_after"] == 8 and data["n_removed"] == 2
    assert data["min_counts"] == 40 and data["min_genes"] == 15
    # No local absolute path crosses.
    assert "/private/patients" not in text


def test_privacy_rejects_an_inconsistent_qc_summary():
    from karospace_agent.privacy import Boundary

    boundary = Boundary(allow_local_paths=True)
    # n_before - n_after != n_removed -> fail closed.
    stdout = 'QC_FILTER_JSON {"n_before": 10, "n_after": 8, "n_removed": 5}'
    raw = {"_local": {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}}
    res = boundary.filter("qc_filter", {}, {}, raw)
    assert res["is_error"] is True
    assert json.loads(res["content"][0]["text"])["diagnostic"] == "schema_unavailable"


def test_privacy_rejects_a_missing_qc_marker():
    from karospace_agent.privacy import Boundary

    boundary = Boundary(allow_local_paths=True)
    raw = {"_local": {"returncode": 0, "stdout": "no marker here", "stderr": "", "timed_out": False}}
    res = boundary.filter("qc_filter", {}, {}, raw)
    assert res["is_error"] is True
    assert json.loads(res["content"][0]["text"])["diagnostic"] == "schema_unavailable"
