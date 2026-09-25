"""Embedding-only regression: existing scientific analysis must survive intact."""
import importlib.util

import numpy as np
import pandas as pd
import pytest

from karospace_agent import commands


def module():
    spec = importlib.util.spec_from_file_location("add_umap", commands.ADD_UMAP_SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def dataset(path):
    ad = pytest.importorskip("anndata")
    sp = pytest.importorskip("scipy.sparse")
    rng = np.random.default_rng(42)
    data = ad.AnnData(sp.csr_matrix(rng.poisson(2, (30, 8)).astype(np.float32)))
    data.obs["annotation"] = pd.Categorical(["type_a", "type_b"] * 15)
    data.var["selected"] = True
    data.layers["counts"] = data.X.copy()
    data.layers["normalized"] = data.X.log1p()
    data.raw = data.copy()
    data.obsm["X_pca"] = rng.normal(size=(30, 5)).astype(np.float32)
    data.obsm["spatial"] = rng.normal(size=(30, 2))
    data.obsp["connectivities"] = sp.eye(30, format="csr")
    data.uns["neighbors"] = {"connectivities_key": "connectivities", "params": {"method": "original"}}
    data.write_h5ad(path)
    return data


def assert_preserved(before, after):
    np.testing.assert_array_equal(before.X.toarray(), after.X.toarray())
    np.testing.assert_array_equal(before.raw.X.toarray(), after.raw.X.toarray())
    pd.testing.assert_frame_equal(before.obs, after.obs)
    pd.testing.assert_frame_equal(before.var, after.var)
    pd.testing.assert_frame_equal(before.raw.var, after.raw.var)
    assert before.uns == after.uns
    assert set(before.layers) == set(after.layers)
    assert set(before.obsp) == set(after.obsp)
    for key in before.layers:
        np.testing.assert_array_equal(before.layers[key].toarray(), after.layers[key].toarray())
    for key in before.obsp:
        np.testing.assert_array_equal(before.obsp[key].toarray(), after.obsp[key].toarray())
    for key in before.obsm:
        np.testing.assert_array_equal(before.obsm[key], after.obsm[key])


def test_computes_only_umap_preserving_analysis_and_source(tmp_path):
    ad = pytest.importorskip("anndata")
    pytest.importorskip("scanpy")
    source, output = tmp_path / "input.h5ad", tmp_path / "output.h5ad"
    dataset(source)
    before = ad.read_h5ad(source)
    original = source.read_bytes()
    result = module().run(source, output, n_neighbors=5)
    after = ad.read_h5ad(output)
    assert result["cells"] == 30
    assert_preserved(before, after)
    assert set(after.obsm) == set(before.obsm) | {"X_umap"}
    assert after.obsm["X_umap"].shape == (30, 2)
    assert np.isfinite(after.obsm["X_umap"]).all()
    assert source.read_bytes() == original


@pytest.mark.parametrize("key", ["X_umap", "umap"])
def test_existing_embedding_kept_without_requiring_pca(tmp_path, key):
    ad = pytest.importorskip("anndata")
    source, output = tmp_path / "input.h5ad", tmp_path / "output.h5ad"
    data = dataset(source)
    del data.obsm["X_pca"]
    embedding = np.arange(60, dtype=float).reshape(30, 2)
    data.obsm[key] = embedding
    data.write_h5ad(source)
    module().run(source, output)
    after = ad.read_h5ad(output)
    assert_preserved(ad.read_h5ad(source), after)
    np.testing.assert_array_equal(after.obsm["X_umap"], embedding)


def test_string_metadata_dtypes_are_preserved(tmp_path):
    ad = pytest.importorskip("anndata")
    source, output = tmp_path / "input.h5ad", tmp_path / "output.h5ad"
    data = dataset(source)
    data.obs["text"] = ["alpha", "beta"] * 15
    data.var["text"] = ["gene"] * 8
    data.raw = data.copy()
    data.obsm["X_umap"] = np.zeros((30, 2))
    data.write_h5ad(source, convert_strings_to_categoricals=False)
    module().run(source, output)
    assert_preserved(ad.read_h5ad(source), ad.read_h5ad(output))


@pytest.mark.parametrize("defect,code", [
    ("missing", "embedding_representation_missing"),
    ("nonfinite", "embedding_representation_invalid"),
    ("bad_existing", "embedding_representation_invalid"),
    ("neighbors", "embedding_parameters_invalid"),
    ("distance", "embedding_parameters_invalid"),
])
def test_invalid_input_never_writes_output(tmp_path, defect, code):
    source, output = tmp_path / "input.h5ad", tmp_path / "output.h5ad"
    data = dataset(source)
    options = {}
    if defect == "missing":
        del data.obsm["X_pca"]
    elif defect == "nonfinite":
        data.obsm["X_pca"][-1, -1] = np.nan
    elif defect == "bad_existing":
        data.obsm["X_umap"] = np.ones((30, 3))
    elif defect == "neighbors":
        options["n_neighbors"] = 1
    else:
        options["min_dist"] = -1
    data.write_h5ad(source)
    with pytest.raises(ValueError, match=code):
        module().run(source, output, **options)
    assert not output.exists()


@pytest.mark.parametrize("kind", ["source", "existing", "symlink"])
def test_output_cannot_overwrite_files(tmp_path, kind):
    source, output = tmp_path / "input.h5ad", tmp_path / "output.h5ad"
    dataset(source)
    if kind == "source":
        output = source
    elif kind == "symlink":
        output.symlink_to(tmp_path / "missing.h5ad")
    else:
        output.write_bytes(b"keep me")
    original = source.read_bytes()
    with pytest.raises(ValueError, match="embedding_output_exists"):
        module().run(source, output)
    assert source.read_bytes() == original
    if kind == "existing":
        assert output.read_bytes() == b"keep me"


def test_wrapper_preserves_explicit_zero_and_representation(monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "merge_python", lambda: "python")
    monkeypatch.setattr(commands, "run", lambda argv, **kw: calls.append(argv))
    commands.run_add_umap("in.h5ad", "out.h5ad", representation="X_scVI", min_dist=0, random_state=0)
    assert calls[0][calls[0].index("--representation") + 1] == "X_scVI"
    assert calls[0][calls[0].index("--min-dist") + 1] == "0"
