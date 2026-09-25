"""Full local scans of synthetic matrices, without exposing any values."""
import importlib.util
import json
from types import SimpleNamespace

import numpy as np
import pytest

from karospace_agent import commands
from karospace_agent.privacy import Boundary


def module():
    spec = importlib.util.spec_from_file_location("check_readiness", commands.READINESS_SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def dataset(path, storage="csr"):
    ad = pytest.importorskip("anndata")
    sp = pytest.importorskip("scipy.sparse")
    x = np.array([[0, 1], [2, 3], [4, 5]], dtype=np.float32)
    if storage != "dense":
        x = getattr(sp, storage + "_matrix")(x)
    data = ad.AnnData(x)
    data.obs["private_section"] = ["PRIVATE_A", "PRIVATE_A", "PRIVATE_B"]
    data.obsm["spatial"] = np.array([[1., 2.], [3., 4.], [5., 6.]])
    data.write_h5ad(path)
    return data


@pytest.mark.parametrize("storage", ["dense", "csr", "csc"])
def test_valid_counts_and_coordinates_are_ready(tmp_path, storage):
    src = tmp_path / "PRIVATE.h5ad"
    dataset(src, storage)
    result = module().check(src, tmp_path, "private_section", require_counts=True)
    assert result["ready"] and result["raw_counts"]
    assert result["cells"] == 3 and result["section_groups"] == 2
    assert "PRIVATE" not in json.dumps(result) and "private_section" not in json.dumps(result)


@pytest.mark.parametrize("defect,code", [
    ("fractional", "raw_counts_required"), ("negative", "raw_counts_required"),
    ("nan", "nonfinite_expression"), ("coords", "invalid_coordinates"),
    ("missing_coords", "spatial_coordinates_missing"), ("missing_section", "section_labels_missing"),
])
def test_invalid_data_is_detected(tmp_path, defect, code):
    src = tmp_path / "input.h5ad"
    data = dataset(src, "dense")
    if defect in {"fractional", "negative", "nan"}:
        data.X[-1, -1] = {"fractional": .5, "negative": -1, "nan": np.nan}[defect]
    elif defect == "coords":
        data.obsm["spatial"][-1, -1] = np.inf
    elif defect == "missing_coords":
        del data.obsm["spatial"]
    else:
        data.obs.loc[data.obs.index[-1], "private_section"] = None
    data.write_h5ad(src)
    probe = module()
    probe.CHUNK = 2  # Ensure the last block is actually scanned.
    result = probe.check(src, tmp_path, "private_section", require_counts=True)
    assert not result["ready"] and code in result["errors"]


def test_normalized_x_can_use_separate_counts_layer(tmp_path):
    src = tmp_path / "input.h5ad"
    data = dataset(src, "dense")
    data.layers["counts"] = data.X.copy()
    data.X = np.log1p(data.X)
    data.write_h5ad(src)
    assert module().check(src, tmp_path, counts_layer="counts", require_counts=True)["ready"]
    assert not module().check(src, tmp_path, require_counts=True)["ready"]


def test_nonspatial_operations_do_not_require_coordinates(tmp_path):
    src = tmp_path / "input.h5ad"
    data = dataset(src)
    del data.obsm["spatial"]
    data.write_h5ad(src)
    assert module().check(src, tmp_path, require_spatial=False, require_counts=True)["ready"]
    assert not module().check(src, tmp_path)["ready"]


@pytest.mark.parametrize("defect", ["none", "missing_pair", "nonfinite", "text"])
def test_obs_coordinates_match_export_requirements(tmp_path, defect):
    src = tmp_path / "input.h5ad"
    data = dataset(src)
    del data.obsm["spatial"]
    data.obs["x"] = [1., 2., 3.]
    data.obs["y"] = [2., 3., 4.]
    if defect == "nonfinite":
        data.obs.loc[data.obs.index[-1], "y"] = np.nan
    elif defect == "text":
        data.obs["y"] = ["PRIVATE"] * 3
    data.write_h5ad(src)
    result = module().check(src, tmp_path, spatial_x="x", spatial_y="" if defect == "missing_pair" else "y")
    assert result["ready"] == (defect == "none")
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("mode,key", [("export", "X_spatial"), ("export", "spatial_coords"),
                                      ("companion", "X_spatial"), ("companion", "obs")])
def test_reader_coordinate_fallbacks_are_checked(tmp_path, mode, key):
    src = tmp_path / "input.h5ad"
    data = dataset(src)
    coords = data.obsm.pop("spatial")
    if key == "obs":
        data.obs["array_col"], data.obs["array_row"] = coords[:, 0], coords[:, 1]
    else:
        data.obsm[key] = coords
    data.write_h5ad(src)
    assert module().check(src, tmp_path, coordinate_mode=mode)["ready"]
    assert not module().check(src, tmp_path)["ready"]
    if key == "obs":
        data.obs.loc[data.obs.index[-1], "array_col"] = np.nan
    else:
        data.obsm[key][-1, -1] = np.nan
    data.write_h5ad(src)
    assert "invalid_coordinates" in module().check(src, tmp_path, coordinate_mode=mode)["errors"]


def test_invalid_primary_coordinates_cannot_hide_behind_valid_fallback(tmp_path):
    src = tmp_path / "input.h5ad"
    data = dataset(src)
    data.obsm["X_spatial"] = data.obsm["spatial"].copy()
    data.obsm["spatial"][-1, -1] = np.inf
    data.write_h5ad(src)
    for mode in ("export", "companion"):
        assert "invalid_coordinates" in module().check(src, tmp_path, coordinate_mode=mode)["errors"]


def test_resource_estimates_warn_without_claiming_guaranteed_failure(tmp_path, monkeypatch):
    src = tmp_path / "input.h5ad"
    dataset(src)
    probe = module()
    monkeypatch.setattr(probe, "available_memory", lambda: 1)
    monkeypatch.setattr(probe.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    result = probe.check(src, tmp_path)
    assert result["ready"]
    assert {"memory_estimate_exceeds_available", "disk_estimate_exceeds_free_space"} <= set(result["warnings"])


def test_zarr_and_ambiguous_spatialdata_tables(tmp_path):
    zarr = pytest.importorskip("zarr")
    data = dataset(tmp_path / "in.h5ad")
    path = tmp_path / "in.zarr"
    data.write_zarr(path)
    assert module().check(path, tmp_path, "private_section")["ready"]
    root = zarr.open_group(str(tmp_path / "ambiguous.zarr"), mode="w")
    root.create_group("tables").create_group("one")
    root["tables"].create_group("two")
    result = module().check(tmp_path / "ambiguous.zarr", tmp_path)
    assert "table_selection_required" in result["errors"]


def test_malformed_sparse_indices_rejected(tmp_path):
    import h5py
    src = tmp_path / "input.h5ad"
    dataset(src)
    with h5py.File(src, "r+") as root:
        root["X/indices"][-1] = 999
    assert "invalid_matrix" in module().check(src, tmp_path)["errors"]


@pytest.mark.parametrize("patch", [{"errors": ["PRIVATE"]}, {"cells": "PRIVATE"},
                                   {"ready": "PRIVATE"}, {"ready": False}, {"raw_counts": 1}])
def test_readiness_boundary_fails_closed(patch):
    summary = {"ready": True, "errors": [], "warnings": [], "raw_counts": True, "cells": 3}
    raw = {"_local": {"returncode": 0, "stdout": "READINESS_JSON " + json.dumps(summary | patch)}}
    result = Boundary().filter("check_readiness", {}, {}, raw)
    assert result["is_error"] and "PRIVATE" not in json.dumps(result)
