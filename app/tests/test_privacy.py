"""Synthetic identifiers must never appear in model-bound tool payloads."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from karospace_agent import agent, codex, tools
from karospace_agent.privacy import Boundary

PRIVATE = "PERSON_SENTINEL_19700101"


def raw(stdout="", stderr="", code=0):
    return {"content": [{"type": "text", "text": PRIVATE}], "is_error": bool(code),
            "_local": {"stdout": stdout, "stderr": stderr, "returncode": code}}


def payload(response):
    assert PRIVATE not in json.dumps(response)
    assert "_local" not in response
    return json.loads(response["content"][0]["text"])


def test_inspection_aliases_paths_columns_and_modality_labels():
    boundary = Boundary()
    text = (f"Input: /private/{PRIVATE}.h5ad\nCells: 1,000\n"
            f"Features by modality:\n  - {PRIVATE} [private label] (default): 50 features\n"
            "Available cell metadata (adata.obs):\n"
            f"  - donor_{PRIVATE} [categorical; 2 values; 1 missing] examples: {PRIVATE}\n")
    data = payload(boundary.filter("inspect_input", {}, {}, raw(text, PRIVATE)))
    column = data["columns"][0]
    assert column["name"] == "col_1"
    assert column["cardinality"] == 2 and column["missing"] == 1
    assert "identifier" in column["role_hints"]
    assert data["cells"] == 1000 and data["modalities"][0]["features"] == 50
    assert boundary.decode({"flags": ["--section-key", "col_1"]})["flags"][1] == f"donor_{PRIVATE}"


def test_structure_and_rds_forward_only_known_typed_fields():
    boundary = Boundary()
    structure = (f"structure: /private/{PRIVATE}.h5ad [h5ad]\n"
                 "X: dtype=float32, format=csr, all_integer=no\n"
                 f"layers:\n  - counts_{PRIVATE}: dtype=int32, format=csr, all_integer=yes\n"
                 f"obsm: spatial_{PRIVATE} (2 cols)\nobsp: {PRIVATE}\nspatial_graph_present: yes\n")
    data = payload(boundary.filter("inspect_structure", {}, {}, raw(structure)))
    assert data["spatial_graph_present"] is True
    assert data["layers"][0]["name"] == "layer_1"
    schema = {"cells": 12, "genes": 15, "has_spatial": True,
              "input": PRIVATE, "object_type": PRIVATE, "unexpected": PRIVATE,
              "available_assays": [PRIVATE], "selected_assay": PRIVATE,
              "available_layers": [f"counts_{PRIVATE}"], "reduced_dims": [f"spatial_{PRIVATE}"]}
    data = payload(boundary.filter("rds_inspect", {}, {}, raw(json.dumps(schema))))
    assert data["selected_assay"] == data["available_assays"][0]["name"]
    assert data["available_layers"][0]["name"] == "layer_1"
    assert data["cells"] == 12


@pytest.mark.parametrize("name", ["inspect_input", "inspect_structure", "rds_inspect", "rds_convert", "geo_manifest", "geo_fetch_file"])
def test_unrecognized_schema_fails_closed(name):
    response = Boundary().filter(name, {}, {}, raw(PRIVATE, PRIVATE))
    assert response["is_error"]
    assert payload(response)["diagnostic"] == "schema_unavailable"


@pytest.mark.parametrize("name", tools.TOOL_NAMES)
def test_every_tool_withholds_errors_and_unexpected_content(name):
    response = Boundary().filter(name, {}, {}, raw(PRIVATE, f"Failure for {PRIVATE}", 2))
    assert response["is_error"]
    assert payload(response)["diagnostic"] == "operation_failed"


@pytest.mark.parametrize("name", ["run_export", "run_preprocess", "run_companion", "merge_sections", "geo_build", "package_sidecar", "generate_notebook"])
def test_successful_commands_do_not_forward_any_logs(name):
    data = payload(Boundary().filter(name, {"output": "/karo/output/view.html"}, {}, raw(PRIVATE, PRIVATE)))
    assert data == {"status": "ok", "exit_code": 0, "output": "/karo/output/view.html"}


def test_geo_files_keep_sample_relationship_without_names():
    boundary = Boundary()
    text = (f"GEO manifest: GSE123\ntitle: {PRIVATE}\n"
            f"GSM123  [xenium]  {PRIVATE}\n  files:\n    - {PRIVATE}.rds  (1 MB)\n"
            f"GSM124  [xenium]  {PRIVATE}\n  files:\n    - {PRIVATE}.zip  (2 MB)\n")
    data = payload(boundary.filter("geo_manifest", {}, {}, raw(text)))
    assert [s["accession"] for s in data["samples"]] == ["GSM123", "GSM124"]
    alias = data["samples"][0]["files"][0]["match"]
    assert boundary.decode({"match": alias})["match"] == PRIVATE + ".rds"


def test_paths_roundtrip_locally_and_preview_approvals_are_single_use(tmp_path):
    boundary = Boundary()
    path = tmp_path / (PRIVATE + " with spaces.h5ad")
    draft = boundary.preview(f"Input file: {path}\nBuild a viewer.")
    assert PRIVATE not in draft["text"] and str(tmp_path) not in draft["text"]
    token = draft["text"].splitlines()[0].removeprefix("Input file: ")
    assert boundary.decode({"input_path": token})["input_path"] == str(path)
    with pytest.raises(ValueError):
        boundary.consume(draft["text"])
    approved = boundary.approve(draft["draft_id"])
    assert boundary.consume(approved) == draft["text"]
    with pytest.raises(ValueError):
        boundary.consume(approved)
    with pytest.raises(ValueError):
        boundary.approve(draft["draft_id"])
    with pytest.raises(ValueError):
        boundary.decode({"input_path": str(path)})
    with pytest.raises(ValueError):
        boundary.decode({"output": "/karo/output/../escape"})


def test_short_schema_names_do_not_corrupt_words():
    boundary = Boundary()
    boundary.alias("x", "col")
    assert boundary.prepare_message("export x") == "export col_1"


def test_help_does_not_publish_unexpected_option_shaped_identifiers():
    data = payload(Boundary().filter("cli_help", {}, {}, raw("--help --patient-jane-doe " + PRIVATE)))
    assert data["options"] == ["--help"]


def test_text_column_type_from_real_inspector_is_recognized():
    data = payload(Boundary().filter("inspect_input", {}, {}, raw(f"  - {PRIVATE} [text; 7 values]")))
    assert data["columns"] == [{"name": "col_1", "type": "text", "cardinality": 7, "missing": 0, "role_hints": []}]


def test_free_text_requires_human_review_not_a_false_pii_detector():
    # Arbitrary names are not recognizable: the exact retained text must be
    # visible in the preview, never silently sent by either backend.
    boundary = Boundary()
    assert boundary.preview(PRIVATE)["text"] == PRIVATE


def test_tool_exceptions_do_not_leave_local_wrapper():
    async def explode(_):
        raise ValueError(PRIVATE)
    response = asyncio.run(Boundary().invoke(SimpleNamespace(handler=explode), {}))
    assert payload(response)["diagnostic"] == "local_tool_error"


def test_codex_tool_rpc_filters_before_writing_and_preserves_local_arguments():
    async def exercise():
        session = codex.Session(on_event=lambda *_: None)
        session._thread, session._turn = "thread", "turn"
        token = session.boundary.register_path(f"/private/{PRIVATE}.h5ad")
        sent = []
        async def worker(name, arguments):
            assert arguments["input_path"] == f"/private/{PRIVATE}.h5ad"
            return raw(f"  - donor_{PRIVATE} [categorical; 2 values] examples: {PRIVATE}")
        async def write(message):
            sent.append(message)
        session._run_tool, session._write = worker, write
        await session._tool_call("call", {"threadId": "thread", "turnId": "turn", "tool": "inspect_input",
                                          "arguments": {"input_path": token, "spatialdata_table": ""}})
        assert PRIVATE not in json.dumps(sent)
        assert sent[0]["result"]["success"]
        assert "col_1" in json.dumps(sent)
    asyncio.run(exercise())


@pytest.mark.parametrize("backend", [agent, codex])
def test_backends_reject_unreviewed_text_before_model_request(backend, monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        async def query(self, text): pytest.fail("unreviewed text reached model")
    monkeypatch.setattr(agent, "ClaudeSDKClient", Client)
    async def exercise():
        session = backend.Session()
        try:
            with pytest.raises(ValueError, match="privacy preview"):
                await session.send(PRIVATE)
            if backend is codex:
                assert session._process is None
        finally:
            if session._workspace:
                session._workspace.cleanup()
    asyncio.run(exercise())
