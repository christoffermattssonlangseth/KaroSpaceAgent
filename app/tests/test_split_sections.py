"""The section-splitter: argv wiring, tool registration, and — when
scikit-learn is available — that auto detection discovers the piece count from
spatial gaps (absorbing sub-threshold specks) while kmeans honours a fixed k.
"""

import importlib.util

import numpy as np
import pytest

from karospace_agent import commands


def load_split():
    spec = importlib.util.spec_from_file_location("split_sections", commands.SPLIT_SECTIONS_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def synthetic(within=None):
    """Two far-apart blobs (120 cells each, above min-cells) plus a 15-cell speck
    off on its own — the speck is below the threshold and must fold into a piece."""
    import anndata as ad
    import scipy.sparse as sp

    rng = np.random.default_rng(0)
    a = rng.normal((0, 0), 20, size=(120, 2))
    b = rng.normal((10_000, 10_000), 20, size=(120, 2))
    speck = rng.normal((10_000, 0), 5, size=(15, 2))
    xy = np.vstack([a, b, speck])
    adata = ad.AnnData(X=sp.csr_matrix(np.ones((xy.shape[0], 3), dtype=np.float32)))
    adata.obsm["spatial"] = xy
    if within:
        adata.obs[within] = "S1"
    return adata


def test_split_script_is_discoverable():
    assert commands.SPLIT_SECTIONS_SCRIPT.exists()


def test_run_split_sections_argv_auto_omits_k(monkeypatch):
    captured = {}
    monkeypatch.setattr(commands, "run", lambda argv, timeout=0: captured.setdefault("argv", argv) or commands.RunResult(0, "", ""))
    monkeypatch.setattr(commands, "merge_python", lambda: "PY")
    commands.run_split_sections("in.h5ad", "out.h5ad", within="sample_id", method="auto", key="section")
    argv = captured["argv"]
    assert argv[:3] == ["PY", str(commands.SPLIT_SECTIONS_SCRIPT), "in.h5ad"]
    assert "--within" in argv and "sample_id" in argv
    assert "--method" in argv and "auto" in argv
    assert "--key" in argv and "section" in argv
    assert "--k" not in argv                         # auto never forces a count


def test_run_split_sections_argv_kmeans_carries_k(monkeypatch):
    captured = {}
    monkeypatch.setattr(commands, "run", lambda argv, timeout=0: captured.setdefault("argv", argv) or commands.RunResult(0, "", ""))
    monkeypatch.setattr(commands, "merge_python", lambda: "PY")
    commands.run_split_sections("in.h5ad", "out.h5ad", method="kmeans", k=3)
    argv = captured["argv"]
    assert "--method" in argv and "kmeans" in argv
    assert "--k" in argv and "3" in argv


def test_split_sections_is_a_registered_tool():
    from karospace_agent import agent, tools

    assert "split_sections" in tools.TOOL_NAMES
    assert "mcp__karospace__split_sections" in agent.ALLOWED_TOOL_NAMES


def test_auto_discovers_two_pieces_and_absorbs_the_speck():
    pytest.importorskip("sklearn")
    mod = load_split()
    labels, groups = mod.split(synthetic(), "spatial", "", "auto", 0,
                               None, 10, 30.0, 100, np.random.default_rng(0))
    # Two big blobs; the 15-cell speck is below min-cells and folds into a piece.
    assert len(set(labels)) == 2
    assert groups == [{"pieces": 2, "sizes": [135, 120]}]
    assert len(labels) == 255 and "" not in set(labels)


def test_auto_falls_back_to_clusters_when_none_clears_min_cells():
    # A tiny genuine capture: every piece is below min-cells. Rather than absorb
    # everything into one, keep the pieces gap detection actually found.
    pytest.importorskip("sklearn")
    mod = load_split()
    labels, groups = mod.split(synthetic(), "spatial", "", "auto", 0,
                               None, 10, 30.0, 10_000, np.random.default_rng(0))
    assert groups[0]["pieces"] >= 2


def test_kmeans_honours_fixed_k():
    pytest.importorskip("sklearn")
    mod = load_split()
    labels, groups = mod.split(synthetic(within="sample_id"), "spatial", "sample_id",
                               "kmeans", 3, None, 10, 30.0, 100, np.random.default_rng(0))
    assert groups[0]["pieces"] == 3
    assert all(lab.startswith("S1__p") for lab in set(labels))


def test_missing_coordinates_raises_the_mapped_phrase():
    import anndata as ad
    import scipy.sparse as sp

    mod = load_split()
    adata = ad.AnnData(X=sp.csr_matrix(np.ones((5, 2), dtype=np.float32)))
    with pytest.raises(SystemExit) as e:
        mod.split(adata, "spatial", "", "auto", 0, None, 10, 30.0, 100, np.random.default_rng(0))
    assert "no spatial coordinates" in str(e.value)   # -> spatial_coordinates_missing
