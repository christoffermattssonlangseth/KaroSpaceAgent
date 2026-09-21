"""The notebook generator: pure schema-level templating (reads no data), correct
structure, CellCharter cells gated by the flag, argv wiring, and tool registration.
"""

import importlib.util
import json

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
