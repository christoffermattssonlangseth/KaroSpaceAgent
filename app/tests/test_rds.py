"""The .rds -> .h5ad conversion tools: binary locator, argv wiring (no R needed),
missing-binary handling, tool registration, and — when R + rds2h5ad + zellkonverter
are available — a tiny end-to-end that must produce an inspectable .h5ad.
"""

import json
import shutil
import subprocess

import pytest

from karospace_agent import agent, commands, tools


# --- Locator ---------------------------------------------------------------

def test_rds2h5ad_bin_prefers_env_override(monkeypatch):
    monkeypatch.setenv("RDS2H5AD_BIN", "/custom/rds2h5ad")
    assert commands.rds2h5ad_bin() == "/custom/rds2h5ad"


def test_rds2h5ad_bin_falls_back_to_path(monkeypatch):
    monkeypatch.delenv("RDS2H5AD_BIN", raising=False)
    monkeypatch.setattr(commands.shutil, "which", lambda name: "/usr/bin/" + name)
    assert commands.rds2h5ad_bin() == "/usr/bin/rds2h5ad"


# --- argv wiring (network / R free) ----------------------------------------

def test_run_rds_inspect_argv(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return commands.RunResult(0, "{}", "")

    monkeypatch.setattr(commands, "run", fake_run)
    monkeypatch.setattr(commands, "rds2h5ad_bin", lambda: "RDS")
    commands.run_rds_inspect("obj.rds", assay="SCT")
    argv = captured["argv"]
    assert argv == ["RDS", "inspect", "obj.rds", "--assay", "SCT"]
    # Schema-only probe must not tee to the console.
    assert captured["kwargs"].get("stream") is False


def test_run_rds_convert_argv_carries_options(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return commands.RunResult(0, "", "")

    monkeypatch.setattr(commands, "run", fake_run)
    monkeypatch.setattr(commands, "rds2h5ad_bin", lambda: "RDS")
    commands.run_rds_convert(
        "obj.rds", "out.h5ad",
        assay="RNA", x_layer="counts",
        reduced_dims=["pca", "umap"], no_spatial=True,
    )
    argv = captured["argv"]
    assert argv[:4] == ["RDS", "convert", "obj.rds", "out.h5ad"]
    assert "--assay" in argv and "RNA" in argv
    assert "--x-layer" in argv and "counts" in argv
    assert "--reduced-dims" in argv and "pca,umap" in argv
    assert "--no-spatial" in argv


def test_run_rds_convert_omits_unset_options(monkeypatch):
    captured = {}
    monkeypatch.setattr(commands, "run", lambda argv, **kw: captured.setdefault("argv", argv) or commands.RunResult(0, "", ""))
    monkeypatch.setattr(commands, "rds2h5ad_bin", lambda: "RDS")
    commands.run_rds_convert("obj.rds", "out.h5ad")
    argv = captured["argv"]
    assert argv == ["RDS", "convert", "obj.rds", "out.h5ad"]


def test_run_rds_validate_argv(monkeypatch):
    captured = {}
    monkeypatch.setattr(commands, "run", lambda argv, **kw: (captured.update(argv=argv, kwargs=kw), commands.RunResult(0, "{}", ""))[1])
    monkeypatch.setattr(commands, "rds2h5ad_bin", lambda: "RDS")
    commands.run_rds_validate("out.h5ad")
    assert captured["argv"] == ["RDS", "validate", "out.h5ad"]
    assert captured["kwargs"].get("stream") is False


def test_run_rds_validate_missing_binary(monkeypatch):
    monkeypatch.setattr(commands, "rds2h5ad_bin", lambda: None)
    rr = commands.run_rds_validate("out.h5ad")
    assert rr.returncode == 127 and "rds2h5ad not found" in rr.stderr


def test_missing_binary_reports_how_to_install(monkeypatch):
    monkeypatch.setattr(commands, "rds2h5ad_bin", lambda: None)
    rr = commands.run_rds_convert("obj.rds", "out.h5ad")
    assert rr.returncode == 127 and not rr.ok
    assert "rds2h5ad not found" in rr.stderr


# --- Registration ----------------------------------------------------------

def test_rds_tools_are_registered_and_allowed():
    for name in ("rds_inspect", "rds_convert", "rds_validate"):
        assert name in tools.TOOL_NAMES
        assert f"mcp__karospace__{name}" in agent.ALLOWED_TOOL_NAMES


# --- validate privacy branch (synthetic JSON, no R needed) -----------------

def test_validate_forwards_names_and_counts_not_the_local_path():
    from karospace_agent.privacy import Boundary

    boundary = Boundary(allow_local_paths=True)
    # jsonlite auto_unbox collapses length-1 vectors to scalars: obs_columns and
    # assays here are single strings, reduced_dims a list, var_columns absent.
    stdout = json.dumps({
        "input": "/private/patients/tiny.h5ad",
        "cells": 8, "genes": 20,
        "assays": "counts",
        "assay_dims": {"name": "counts", "dims": [20, 8]},
        "reduced_dims": ["harmonyX", "spatial"],
        "obs_columns": "cell_type",
        "spatial_dims": [8, 2],
    })
    raw = {"_local": {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}}
    res = boundary.filter("rds_validate", {}, {}, raw)
    text = res["content"][0]["text"]
    data = json.loads(text)

    assert res["is_error"] is False
    assert data["cells"] == 8 and data["genes"] == 20
    assert data["has_spatial"] is True and data["spatial_dims"] == [8, 2]
    # Scalars were coerced to single-element lists and every name aliased.
    assert len(data["assays"]) == 1 and data["assays"][0]["dims"] == [20, 8]
    assert len(data["obs_columns"]) == 1
    assert len(data["reduced_dims"]) == 2
    # The real names and the local input path never cross. (Role hints like
    # "spatial" are fixed vocabulary and may appear by design; the embedding's
    # actual name "harmonyX" must not.)
    for leak in ("counts", "cell_type", "harmonyX", "/private/patients", "tiny.h5ad"):
        assert leak not in text


def test_validate_fails_closed_on_a_non_dict_summary():
    from karospace_agent.privacy import Boundary

    boundary = Boundary(allow_local_paths=True)
    raw = {"_local": {"returncode": 0, "stdout": "[1, 2, 3]", "stderr": "", "timed_out": False}}
    res = boundary.filter("rds_validate", {}, {}, raw)
    assert res["is_error"] is True
    assert json.loads(res["content"][0]["text"])["diagnostic"] == "schema_unavailable"


# --- End-to-end (only when the R backend is really available) --------------

def _rds_backend_ready() -> bool:
    if not (commands.rds2h5ad_bin() and shutil.which("Rscript")):
        return False
    probe = subprocess.run(
        ["Rscript", "-e",
         'quit(status = as.integer(!all(vapply('
         'c("SingleCellExperiment","zellkonverter"), requireNamespace, logical(1),'
         ' quietly = TRUE))))'],
        capture_output=True, text=True,
    )
    return probe.returncode == 0


rds_ready = pytest.mark.skipif(
    not _rds_backend_ready(),
    reason="needs rds2h5ad + Rscript + SingleCellExperiment + zellkonverter",
)


@pytest.fixture
def tiny_sce_rds(tmp_path):
    """Write a minimal SingleCellExperiment (counts + a colData label) to .rds."""
    rds = tmp_path / "tiny.rds"
    script = f"""
    suppressMessages(library(SingleCellExperiment))
    set.seed(1)
    counts <- matrix(rpois(20 * 8, 3), nrow = 20, ncol = 8)
    rownames(counts) <- paste0("Gene", seq_len(20))
    colnames(counts) <- paste0("Cell", seq_len(8))
    sce <- SingleCellExperiment(assays = list(counts = counts))
    sce$cell_type <- rep(c("A", "B"), length.out = 8)
    saveRDS(sce, "{rds}")
    """
    r = subprocess.run(["Rscript", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return rds


@rds_ready
def test_inspect_reports_schema_only(tiny_sce_rds):
    rr = commands.run_rds_inspect(str(tiny_sce_rds))
    assert rr.ok, rr.stderr
    # Schema facts present; no data values (e.g. no raw count integers dumped).
    assert '"cells": 8' in rr.stdout
    assert '"genes": 20' in rr.stdout
    assert "counts" in rr.stdout


@rds_ready
def test_convert_writes_an_ingestible_h5ad(tiny_sce_rds, tmp_path):
    out = tmp_path / "tiny.h5ad"
    rr = commands.run_rds_convert(str(tiny_sce_rds), str(out))
    assert rr.ok, rr.stderr
    assert out.exists() and out.stat().st_size > 0
    # The converted file is a real HDF5 .h5ad carrying the AnnData groups.
    with open(out, "rb") as fh:
        assert fh.read(8) == b"\x89HDF\r\n\x1a\n"
    # The convert summary reports the schema of what was written (schema, not values).
    summary = json.loads(rr.stdout)
    assert summary["cells"] == 8 and summary["genes"] == 20
    assert summary["x_name"] == "counts"
