"""Display sections must not silently become independent biological replicates."""
import asyncio
import importlib.util

import anndata as ad
import numpy as np
import pytest

from karospace_agent import commands, tools


def guard():
    spec = importlib.util.spec_from_file_location("check_pseudobulk", commands.PSEUDOBULK_CHECK_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("flags", [["--pseudobulk"], ["--pseudobulk=auto"],
                                  ["--pseudobulk", "auto", "--pseudobulk-replicate-annotation="]])
def test_export_never_runs_without_explicit_replicates(monkeypatch, flags):
    monkeypatch.setattr(commands, "run_karospace", lambda *_: pytest.fail("export must be blocked"))
    response = asyncio.run(tools.run_export.handler({"input_path": "synthetic.h5ad", "output": "out.html", "flags": flags}))
    assert response["is_error"]
    assert "pseudobulk_replicate_required" in str(response)


def test_pseudobulk_off_does_not_need_scientific_guard(monkeypatch):
    monkeypatch.setattr(commands, "run", lambda *_args, **_kw: pytest.fail("guard unnecessary"))
    assert commands.check_pseudobulk("unused.h5ad", []) is None
    assert commands.check_pseudobulk("unused.h5ad", ["--pseudobulk=off"]) is None


def test_local_check_refuses_pieces_but_accepts_original_donor(tmp_path):
    source = tmp_path / "split.h5ad"
    data = ad.AnnData(np.ones((4, 2)))
    data.obs["donor"] = ["a", "a", "b", "b"]
    data.obs["pieces"] = ["p1", "p2", "p3", "p4"]
    data.uns["karospace_section_split"] = {"section_keys": ["pieces"]}
    data.write_h5ad(source)
    module = guard()
    assert module.validate(str(source), "pieces") == "pseudobulk_piece_replicate"
    assert module.validate(str(source), "donor") is None
    assert module.validate(str(source), "missing") == "pseudobulk_replicate_required"


def test_failed_provenance_check_blocks_export(monkeypatch):
    monkeypatch.setattr(commands, "check_pseudobulk", lambda *_: commands.RunResult(2, "pseudobulk_piece_replicate", ""))
    monkeypatch.setattr(commands, "run_karospace", lambda *_: pytest.fail("export must be blocked"))
    response = asyncio.run(tools.run_export.handler({"input_path": "synthetic.h5ad", "output": "out.html", "flags": []}))
    assert response["is_error"]


def test_replicate_override_is_discoverable_through_private_help():
    from karospace_agent.privacy import Boundary
    response = Boundary().filter("cli_help", {}, {}, {"_local": {
        "stdout": "--pseudobulk-replicate-annotation", "returncode": 0}})
    assert "--pseudobulk-replicate-annotation" in str(response)
