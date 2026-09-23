"""The section-split PREVIEW: argv wiring, tool registration, that it renders one
PNG panel per capture group with the split proposal, and — crucially — that the
privacy layer forwards only aggregate piece counts, never a panel path or image.
"""

import importlib.util
import json
import sys

import numpy as np
import pytest

from karospace_agent import commands


def load_preview():
    spec = importlib.util.spec_from_file_location("preview_sections", commands.PREVIEW_SECTIONS_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def two_piece_capture():
    """One sample_id holding two far-apart tissue blobs (auto should find 2)."""
    import anndata as ad
    import scipy.sparse as sp

    rng = np.random.default_rng(0)
    a = rng.normal((0, 0), 20, size=(120, 2))
    b = rng.normal((10_000, 10_000), 20, size=(120, 2))
    xy = np.vstack([a, b])
    adata = ad.AnnData(X=sp.csr_matrix(np.ones((xy.shape[0], 3), dtype=np.float32)))
    adata.obsm["spatial"] = xy
    adata.obs["sample_id"] = "S1"
    return adata


def test_preview_script_is_discoverable():
    assert commands.PREVIEW_SECTIONS_SCRIPT.exists()


def test_run_preview_argv_auto_omits_k(monkeypatch):
    captured = {}
    monkeypatch.setattr(commands, "run", lambda argv, timeout=0: captured.setdefault("argv", argv) or commands.RunResult(0, "", ""))
    monkeypatch.setattr(commands, "merge_python", lambda: "PY")
    commands.run_preview_sections("in.h5ad", "outdir", within="sample_id", method="auto")
    argv = captured["argv"]
    assert argv[:3] == ["PY", str(commands.PREVIEW_SECTIONS_SCRIPT), "in.h5ad"]
    assert "--out-dir" in argv and "outdir" in argv
    assert "--within" in argv and "sample_id" in argv
    assert "--method" in argv and "auto" in argv
    assert "--k" not in argv                         # auto never forces a count


def test_run_preview_argv_kmeans_carries_k(monkeypatch):
    captured = {}
    monkeypatch.setattr(commands, "run", lambda argv, timeout=0: captured.setdefault("argv", argv) or commands.RunResult(0, "", ""))
    monkeypatch.setattr(commands, "merge_python", lambda: "PY")
    commands.run_preview_sections("in.h5ad", "outdir", method="kmeans", k=3)
    argv = captured["argv"]
    assert "--method" in argv and "kmeans" in argv
    assert "--k" in argv and "3" in argv


def test_preview_sections_is_a_registered_tool():
    from karospace_agent import agent, tools

    assert "preview_sections" in tools.TOOL_NAMES
    assert "mcp__karospace__preview_sections" in agent.ALLOWED_TOOL_NAMES


def test_preview_renders_a_panel_per_group_and_emits_markers(tmp_path, capsys, monkeypatch):
    pytest.importorskip("sklearn")
    pytest.importorskip("matplotlib")
    src = tmp_path / "in.h5ad"
    two_piece_capture().write_h5ad(src)
    out = tmp_path / "prev"
    mod = load_preview()
    monkeypatch.setattr(sys, "argv", ["preview", str(src), "--out-dir", str(out), "--within", "sample_id"])
    mod.main()

    captured = capsys.readouterr().out
    assert (out / "panel_1.png").exists()

    img_lines = [l for l in captured.splitlines() if l.startswith(mod.IMG_MARKER)]
    assert len(img_lines) == 1
    marker = json.loads(img_lines[0][len(mod.IMG_MARKER):])
    assert marker["group"] == 1 and marker["pieces"] == 2
    assert marker["path"].endswith("panel_1.png")

    summary_line = next(l for l in captured.splitlines() if l.startswith("PREVIEW_SECTIONS_JSON "))
    summary = json.loads(summary_line[len("PREVIEW_SECTIONS_JSON "):])
    assert summary["n_groups"] == summary["n_panels"] == 1
    assert summary["groups"] == [{"pieces": 2}]


def test_zarr_input_is_rejected(monkeypatch):
    mod = load_preview()
    monkeypatch.setattr(sys, "argv", ["preview", "data.zarr", "--out-dir", "prev"])
    with pytest.raises(SystemExit, match="split_input_unsupported"):
        mod.main()


def test_privacy_forwards_only_aggregate_piece_counts():
    from karospace_agent.privacy import Boundary

    boundary = Boundary(allow_local_paths=True)
    stdout = (
        'KAROSPACE_PREVIEW_IMG {"path": "/private/output/panel_1.png", "group": 1, "pieces": 3}\n'
        'PREVIEW_SECTIONS_JSON {"n_groups": 1, "n_panels": 1, "method": "auto", "groups": [{"pieces": 3}]}'
    )
    raw = {"_local": {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}}
    res = boundary.filter("preview_sections", {"output_dir": "/karo/output/section_preview"}, {}, raw)

    text = res["content"][0]["text"]
    assert res["is_error"] is False
    data = json.loads(text)
    assert data["n_groups"] == 1 and data["n_panels"] == 1
    assert data["groups"] == [{"pieces": 3}]
    assert data["requires_local_review"] is True
    # No panel path, image, or absolute local path ever crosses.
    assert "panel_1.png" not in text
    assert "/private/output" not in text
    assert "data:image" not in text
    assert "path" not in data


def test_privacy_rejects_a_malformed_preview_summary():
    from karospace_agent.privacy import Boundary

    boundary = Boundary(allow_local_paths=True)
    stdout = 'PREVIEW_SECTIONS_JSON {"n_groups": 2, "n_panels": 2, "method": "auto", "groups": [{"pieces": 1}]}'
    raw = {"_local": {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}}
    res = boundary.filter("preview_sections", {}, {}, raw)
    # group count disagrees with n_groups -> fail closed.
    assert res["is_error"] is True
    assert json.loads(res["content"][0]["text"])["diagnostic"] == "schema_unavailable"
