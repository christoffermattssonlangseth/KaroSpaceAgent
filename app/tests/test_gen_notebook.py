"""The notebook generator: pure schema-level templating (reads no data), correct
structure, CellCharter cells gated by the flag, argv wiring, and tool registration.
"""

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import anndata as ad
import numpy as np
import pandas as pd
import scipy.sparse as sp

import pytest

from karospace_agent import commands


def load_gen():
    spec = importlib.util.spec_from_file_location("gen_notebook", commands.GEN_NOTEBOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = load_gen()


def test_script_is_discoverable():
    assert commands.GEN_NOTEBOOK_SCRIPT.exists()


def test_build_notebook_reads_no_data_and_embeds_params():
    # A path that does not exist: templating must not touch the file.
    nb = gen.build_notebook(
        "/does/not/exist/raw.h5ad",
        section_key="sample_id",
        resolution=0.6,
        genes=["Cd4", "Cd8a"],
    )
    src = "".join("".join(c["source"]) for c in nb["cells"])
    assert "/does/not/exist/raw.h5ad" in src
    assert "'sample_id'" in src
    assert "0.6" in src
    assert "['Cd4', 'Cd8a']" in src
    # Ends by pointing back at the build with the annotated file.
    assert "raw_annotated.h5ad" in src
    assert "karospace-agent build" in src


def test_cellcharter_cells_are_gated_by_the_flag():
    with_cc = gen.build_notebook("x.h5ad", include_cellcharter=True)
    without = gen.build_notebook("x.h5ad", include_cellcharter=False)
    assert "cellcharter" in "".join("".join(c["source"]) for c in with_cc["cells"]).lower()
    assert "cellcharter" not in "".join("".join(c["source"]) for c in without["cells"]).lower()
    # leiden is always present; spatial_domain only with CellCharter.
    assert "spatial_domain" in "".join("".join(c["source"]) for c in with_cc["cells"])
    assert "spatial_domain" not in "".join("".join(c["source"]) for c in without["cells"])


def test_notebook_is_well_formed_nbformat_v4():
    nb = gen.build_notebook("x.h5ad")
    assert nb["nbformat"] == 4
    for c in nb["cells"]:
        assert c["cell_type"] in ("markdown", "code")
        if c["cell_type"] == "code":
            assert c["execution_count"] is None and c["outputs"] == []
    # Round-trips as JSON, and validates if nbformat is installed.
    text = json.dumps(nb, indent=1)
    reparsed = json.loads(text)
    assert reparsed["cells"]
    nbf = pytest.importorskip("nbformat")
    nbf.validate(reparsed)  # raises if malformed


def test_run_gen_notebook_argv(monkeypatch):
    captured = {}

    def fake_run(argv, timeout=0):
        captured["argv"] = argv
        return commands.RunResult(0, "", "")

    monkeypatch.setattr(commands, "run", fake_run)
    monkeypatch.setattr(commands, "merge_python", lambda: "PY")
    commands.run_gen_notebook(
        "in.h5ad", "nb.ipynb", section_key="sample_id",
        genes=["Cd4"], include_cellcharter=False,
    )
    argv = captured["argv"]
    assert argv[:3] == ["PY", str(commands.GEN_NOTEBOOK_SCRIPT), "in.h5ad"]
    assert "-o" in argv and "nb.ipynb" in argv
    assert "--section-key" in argv and "sample_id" in argv
    assert "--genes" in argv and "Cd4" in argv
    assert "--no-cellcharter" in argv


def test_generate_notebook_is_a_registered_tool():
    from karospace_agent import agent, tools

    assert "generate_notebook" in tools.TOOL_NAMES
    assert "mcp__karospace__generate_notebook" in agent.ALLOWED_TOOL_NAMES


def code_cells(path="synthetic.h5ad", **kwargs):
    return ["".join(c["source"]) for c in gen.build_notebook(str(path), **kwargs)["cells"] if c["cell_type"] == "code"]


def test_every_generated_cell_compiles():
    for cc in (True, False):
        for i, source in enumerate(code_cells(include_cellcharter=cc)):
            compile(source, f"cell_{i}", "exec")


def test_scvi_selects_raw_hvgs_independently_and_writes_nullable_checkpoint(tmp_path, monkeypatch):
    data = ad.AnnData(sp.csr_matrix(np.ones((3, 6000), dtype=np.float32)))
    data.layers["counts"] = data.X.copy()
    data.var["highly_variable"] = [True] * 2000 + [False] * 4000
    data.obs["nullable"] = pd.array(["a", None, "b"], dtype="string")
    selected = np.arange(6000) >= 1000
    calls = []
    def hvg(a, **kw):
        calls.append(kw)
        assert kw["layer"] == "counts" and kw["flavor"] == "seurat_v3" and not kw["inplace"]
        return pd.DataFrame({"highly_variable": selected}, index=a.var_names)
    class Model:
        @staticmethod
        def setup_anndata(a, **kw):
            assert a.n_vars == 5000 and a.var_names[0] == "1000"
        def __init__(self, a): pass
        def train(self, **kw): pass
        def get_latent_representation(self): return np.zeros((3, 2), dtype=np.float32)
    fake_sc = SimpleNamespace(read_h5ad=lambda _: data, pp=SimpleNamespace(highly_variable_genes=hvg))
    monkeypatch.setitem(sys.modules, "scanpy", fake_sc)
    monkeypatch.setattr(ad.settings, "allow_write_nullable_strings", False)
    cells = code_cells(tmp_path / "raw.h5ad")
    namespace = {}
    exec(cells[0], namespace)  # imports/parameters must enable checkpoint compatibility
    assert ad.settings.allow_write_nullable_strings
    checkpoint = tmp_path / "checkpoint.h5ad"
    namespace.update(scvi=SimpleNamespace(model=SimpleNamespace(SCVI=Model)),
                     LIBRARY_KEY=None, ACCELERATOR="cpu", _SCVI_CKPT=str(checkpoint),
                     is_raw_counts=lambda matrix: np.all(matrix.data == 1))
    exec(next(s for s in cells if s.startswith("# --- scVI latent")), namespace)
    assert len(calls) == 1
    written = ad.read_h5ad(checkpoint)
    assert "X_scVI" in written.obsm
    assert written.obs["nullable"].isna().sum() == 1
    assert written.var["scvi_highly_variable"].tolist() == selected.tolist()
    assert written.var["highly_variable"].sum() == 2000


def test_per_library_aggregation_preserves_interleaved_rows_without_expression_copies(tmp_path):
    data = ad.AnnData(sp.csr_matrix(np.ones((4, 100))))
    data.obs["library"] = pd.Categorical(["a", "b", "a", "b"])
    data.obsm["X_scVI"] = np.arange(8).reshape(4, 2).astype(np.float32)
    adjacency = sp.csr_matrix(np.array([[0,0,1,0], [0,0,0,1], [1,0,0,0], [0,1,0,0]]))
    data.obsp["spatial_connectivities"] = adjacency
    calls = []
    def aggregate(sub, **kwargs):
        assert sub.n_vars == 0 and not sub.layers
        rep = sub.obsm[kwargs["use_rep"]]
        graph = sub.obsp[kwargs["connectivity_key"]]
        values = [rep]
        for _ in range(kwargs["n_layers"]):
            values.append(graph @ values[-1])
        sub.obsm[kwargs["out_key"]] = np.hstack(values)
        calls.append(sub.n_obs)
    checkpoint = tmp_path / "agg.h5ad"
    namespace = dict(adata=data, ad=ad, sp=sp, np=np, LIBRARY_KEY="library",
                     cc=SimpleNamespace(gr=SimpleNamespace(aggregate_neighbors=aggregate)), _AGG_CKPT=str(checkpoint))
    source = next(s for s in code_cells() if s.startswith("# --- neighbourhood aggregation"))
    exec(source, namespace)
    expected = [data.obsm["X_scVI"]]
    for _ in range(3): expected.append(adjacency @ expected[-1])
    np.testing.assert_allclose(data.obsm["X_cellcharter"], np.hstack(expected))
    assert calls == [2, 2] and checkpoint.exists()


def test_stability_sweep_requires_review_before_primary_annotation_and_save(tmp_path):
    data = ad.AnnData(np.ones((20, 2)))
    data.obsm["X_cellcharter"] = np.ones((20, 4))
    events = []
    class AutoK:
        best_k = 3
        def __init__(self, **kw):
            assert kw["n_clusters"] == (2, 4) and kw["max_runs"] == 5
            events.append("created")
        def fit(self, a, **kw): events.append("fit")
        def save(self, path): events.append("saved")
        def predict(self, a, use_rep, k):
            return pd.Categorical(np.arange(a.n_obs) % k)
    namespace = dict(adata=data, np=np, ad=ad, _P=Path, DOMAINS=[2,3,4], PRIMARY_K=None,
                     ACCELERATOR="cpu", ANNOTATED_OUTPUT=str(tmp_path / "annotated.h5ad"),
                     cc=SimpleNamespace(tl=SimpleNamespace(ClusterAutoK=AutoK, Cluster=object),
                                        pl=SimpleNamespace(autok_stability=lambda _: events.append("stability_plot"))),
                     sc=SimpleNamespace(pl=SimpleNamespace(embedding=lambda *_a, **_kw: events.append("spatial_plots"))))
    cells = code_cells(domains_min=2, domains_max=4)
    exec(next(s for s in cells if s.startswith("# --- CellCharter stability")), namespace)
    assert events == ["created", "fit", "saved", "stability_plot", "spatial_plots"]
    selection = next(s for s in cells if s.startswith("# Set PRIMARY_K"))
    with pytest.raises(ValueError, match="PRIMARY_K"):
        exec(selection, namespace)
    assert "spatial_domain" not in data.obs
    with pytest.raises(ValueError, match="Review"):
        exec(cells[-1], namespace)
    assert not Path(namespace["ANNOTATED_OUTPUT"]).exists()
    namespace["PRIMARY_K"] = 3
    exec(selection, namespace)
    exec(cells[-1], namespace)
    saved = ad.read_h5ad(namespace["ANNOTATED_OUTPUT"])
    assert saved.obs["spatial_domain"].equals(saved.obs["CellCharter_3"])
