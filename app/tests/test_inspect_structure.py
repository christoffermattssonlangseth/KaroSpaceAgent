"""The structural probe's boundary-relevant surface: its report contains only
schema/aggregates, routes .zarr vs .h5ad, and degrades to an error string rather
than crashing. The numpy/h5py/zarr reads run only under the karospace
interpreter, so these tests exercise the numpy-free formatting and routing by
stubbing the probe functions."""

import importlib.util

import pytest

from karospace_agent import commands


def load_probe():
    spec = importlib.util.spec_from_file_location("inspect_structure", commands.STRUCTURE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


probe = load_probe()


def test_fmt_from_encoding_maps_sparse_kinds():
    assert probe._fmt_from_encoding("csr_matrix") == "csr"
    assert probe._fmt_from_encoding("csc_matrix") == "csc"
    assert probe._fmt_from_encoding("") == "sparse"
    assert probe._fmt_from_encoding("weird") == "weird"


def test_fmt_matrix_absent_and_present():
    assert probe._fmt_matrix(None) == "absent"
    assert probe._fmt_matrix({}) == "absent"
    text = probe._fmt_matrix({"dtype": "float32", "format": "csr", "all_integer": "no"})
    assert text == "dtype=float32, format=csr, all_integer=no"


def test_report_flags_spatial_graph_present():
    r = {
        "path": "/d/x.h5ad",
        "kind": "h5ad",
        "X": {"dtype": "float32", "format": "csr", "all_integer": "no"},
        "layers": {"counts": {"dtype": "int32", "format": "csr", "all_integer": "yes"}},
        "obsm": {"spatial": "2", "X_pca": "50"},
        "obsp": ["spatial_connectivities", "spatial_distances"],
    }
    out = probe.format_report(r)
    assert "spatial_graph_present: yes" in out
    assert "counts: dtype=int32" in out
    assert "spatial (2 cols)" in out
    # The report is schema only — no cell values, no example labels.
    assert "examples" not in out


def test_report_flags_spatial_graph_absent_and_empty_sections():
    r = {
        "path": "/d/x.h5ad",
        "kind": "h5ad",
        "X": {"dtype": "int32", "format": "dense", "all_integer": "yes"},
        "layers": {},
        "obsm": {},
        "obsp": [],
    }
    out = probe.format_report(r)
    assert "spatial_graph_present: no" in out
    assert "layers: (none)" in out
    assert "obsm: (none)" in out
    assert "obsp: (none)" in out


def test_report_includes_raw_x_when_present():
    r = {
        "path": "/d/x.h5ad",
        "kind": "h5ad",
        "X": {"dtype": "float32", "format": "csr", "all_integer": "no"},
        "raw_X": {"dtype": "int32", "format": "csr", "all_integer": "yes"},
        "layers": {},
        "obsm": {},
        "obsp": [],
    }
    out = probe.format_report(r)
    assert "raw.X: dtype=int32" in out


def test_main_routes_h5ad_and_prints_report(monkeypatch, capsys):
    monkeypatch.setattr(
        probe, "probe_h5ad",
        lambda path: {"path": path, "kind": "h5ad", "X": None,
                      "layers": {}, "obsm": {}, "obsp": []},
    )
    monkeypatch.setattr(probe.sys, "argv", ["prog", "/d/x.h5ad"])
    assert probe.main() == 0
    out = capsys.readouterr().out
    assert "structure: /d/x.h5ad" in out and "X: absent" in out


def test_main_routes_zarr_with_table(monkeypatch, capsys):
    seen = {}

    def fake_zarr(path, table=""):
        seen["path"], seen["table"] = path, table
        return {"path": path, "kind": "zarr", "X": None, "layers": {}, "obsm": {}, "obsp": []}

    monkeypatch.setattr(probe, "probe_zarr", fake_zarr)
    monkeypatch.setattr(probe.sys, "argv", ["prog", "/d/store.zarr", "--table", "table"])
    assert probe.main() == 0
    assert seen == {"path": "/d/store.zarr", "table": "table"}


def test_main_reports_failure_without_crashing(monkeypatch, capsys):
    def boom(path):
        raise RuntimeError("bad file")

    monkeypatch.setattr(probe, "probe_h5ad", boom)
    monkeypatch.setattr(probe.sys, "argv", ["prog", "/d/x.h5ad"])
    assert probe.main() == 1
    assert "structure probe failed" in capsys.readouterr().err


def test_main_reports_missing_dependency(monkeypatch, capsys):
    def no_dep(path):
        raise ImportError("No module named 'h5py'")

    monkeypatch.setattr(probe, "probe_h5ad", no_dep)
    monkeypatch.setattr(probe.sys, "argv", ["prog", "/d/x.h5ad"])
    assert probe.main() == 3
    assert "structure probe unavailable" in capsys.readouterr().err
