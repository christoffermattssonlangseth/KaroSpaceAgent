"""The clustering preprocess step: argv wiring (network/scanpy-free), the
loud-failure on an unsupported method, tool registration, and — when scanpy is
available — a tiny end-to-end run that must preserve raw counts and add obs['leiden'].
"""

import importlib.util

import pytest

from karospace_agent import commands


def load_preprocess():
    spec = importlib.util.spec_from_file_location("preprocess", commands.PREPROCESS_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_preprocess_script_is_discoverable():
    assert commands.PREPROCESS_SCRIPT.exists()


def test_run_preprocess_argv_carries_params(monkeypatch):
    captured = {}

    def fake_run(argv, timeout=0):
        captured["argv"] = argv
        return commands.RunResult(0, "", "")

    monkeypatch.setattr(commands, "run", fake_run)
    monkeypatch.setattr(commands, "merge_python", lambda: "PY")
    commands.run_preprocess("in.h5ad", "out.h5ad", resolution=0.5, key="clusters")
    argv = captured["argv"]
    assert argv[:3] == ["PY", str(commands.PREPROCESS_SCRIPT), "in.h5ad"]
    assert "-o" in argv and "out.h5ad" in argv
    assert "--resolution" in argv and "0.5" in argv
    assert "--key" in argv and "clusters" in argv
    # UMAP is on by default, so no opt-out flag is passed.
    assert "--no-umap" not in argv


def test_run_preprocess_argv_opts_out_of_umap(monkeypatch):
    captured = {}

    def fake_run(argv, timeout=0):
        captured["argv"] = argv
        return commands.RunResult(0, "", "")

    monkeypatch.setattr(commands, "run", fake_run)
    monkeypatch.setattr(commands, "merge_python", lambda: "PY")
    commands.run_preprocess("in.h5ad", "out.h5ad", compute_umap=False)
    assert "--no-umap" in captured["argv"]


def test_unsupported_method_fails_loudly_without_scanpy():
    # The method guard runs before any heavy import, so this needs no scanpy.
    pre = load_preprocess()
    with pytest.raises(ValueError) as e:
        pre.run_preprocess("x.h5ad", "o.h5ad", method="cellcharter")
    assert "cellcharter" in str(e.value) and "leiden" in str(e.value)


def test_run_preprocess_is_a_registered_tool():
    from karospace_agent import agent, tools

    assert "run_preprocess" in tools.TOOL_NAMES
    assert "mcp__karospace__run_preprocess" in agent.ALLOWED_TOOL_NAMES


def test_end_to_end_adds_leiden_and_preserves_raw_counts(tmp_path):
    sc = pytest.importorskip("scanpy")  # noqa: F841
    import anndata as ad
    import numpy as np
    import scipy.sparse as sp

    pre = load_preprocess()
    rng = np.random.default_rng(0)
    # Two blobs so leiden finds >1 cluster; raw integer counts.
    counts = np.vstack([
        rng.poisson(1.0, size=(60, 40)),
        rng.poisson(5.0, size=(60, 40)),
    ]).astype(np.float32)
    a = ad.AnnData(X=sp.csr_matrix(counts))
    a.obs_names = [f"c{i}" for i in range(120)]
    a.var_names = [f"g{j}" for j in range(40)]
    inp = tmp_path / "raw.h5ad"
    a.write_h5ad(inp)

    out = tmp_path / "clustered.h5ad"
    log = pre.run_preprocess(str(inp), str(out), resolution=1.0, n_hvg=2000)

    assert out.exists()
    r = ad.read_h5ad(out)
    assert "leiden" in r.obs.columns
    assert "counts" in r.layers and "normalized" in r.layers
    # A 2D UMAP the viewer auto-detects is written when the input lacks one.
    assert "X_umap" in r.obsm and r.obsm["X_umap"].shape == (120, 2)
    # X must be restored to raw integer counts for ingestion.
    x = r.X.data if sp.issparse(r.X) else np.asarray(r.X).ravel()
    assert np.all(np.mod(x, 1) == 0)
    # The log is aggregate-only: mentions cluster count, never a cell label.
    assert any("clusters" in line for line in log)


def test_existing_umap_is_kept_not_recomputed(tmp_path):
    pytest.importorskip("scanpy")
    import anndata as ad
    import numpy as np
    import scipy.sparse as sp

    pre = load_preprocess()
    rng = np.random.default_rng(1)
    counts = rng.poisson(2.0, size=(80, 30)).astype(np.float32)
    a = ad.AnnData(X=sp.csr_matrix(counts))
    a.obs_names = [f"c{i}" for i in range(80)]
    a.var_names = [f"g{j}" for j in range(30)]
    # A sentinel embedding the author already shipped — must survive untouched.
    sentinel = np.arange(80 * 2, dtype=np.float32).reshape(80, 2)
    a.obsm["X_umap"] = sentinel.copy()
    inp = tmp_path / "raw.h5ad"
    a.write_h5ad(inp)

    out = tmp_path / "clustered.h5ad"
    log = pre.run_preprocess(str(inp), str(out))

    r = ad.read_h5ad(out)
    assert np.array_equal(np.asarray(r.obsm["X_umap"]), sentinel)
    assert any("already present" in line for line in log)


def test_no_umap_flag_skips_embedding(tmp_path):
    pytest.importorskip("scanpy")
    import anndata as ad
    import numpy as np
    import scipy.sparse as sp

    pre = load_preprocess()
    rng = np.random.default_rng(2)
    counts = rng.poisson(2.0, size=(80, 30)).astype(np.float32)
    a = ad.AnnData(X=sp.csr_matrix(counts))
    a.obs_names = [f"c{i}" for i in range(80)]
    a.var_names = [f"g{j}" for j in range(30)]
    inp = tmp_path / "raw.h5ad"
    a.write_h5ad(inp)

    out = tmp_path / "clustered.h5ad"
    pre.run_preprocess(str(inp), str(out), compute_umap=False)

    r = ad.read_h5ad(out)
    assert "X_umap" not in r.obsm
