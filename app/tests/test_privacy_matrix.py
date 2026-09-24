"""Every registered tool must withhold synthetic private data on both paths.

The shared invoke path is used by Claude and standalone MCP; Codex has its own
RPC envelope. A new tool must add a recognized success fixture to this matrix.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from karospace_agent import codex, tools
from karospace_agent.privacy import Boundary


PRIVATE = "PRIVATE_PATIENT_19700101"
RAW_IMAGE = "SYNTHETIC_PRIVATE_IMAGE_BYTES"


def marked(marker, data):
    return marker + " " + json.dumps({**data, "unexpected": PRIVATE})


R_SCHEMA = json.dumps({"cells": 5, "genes": 3, "has_spatial": True,
                       "available_assays": [PRIVATE], "available_layers": [PRIVATE],
                       "selected_assay": PRIVATE, "x_name": PRIVATE,
                       "reduced_dims": [PRIVATE], "input": PRIVATE, "unexpected": PRIVATE})
SUCCESS = {
    "inspect_input": f"Cells: 5\n  - donor_{PRIVATE} [categorical; 2 values] examples: {PRIVATE}",
    "inspect_structure": f"X: dtype=float32, format=csr, all_integer=yes\n"
                         f"layers:\n  - {PRIVATE}: dtype=int32, format=csr, all_integer=yes\n"
                         f"obsm: {PRIVATE} (2 cols)\nspatial_graph_present: yes",
    "check_readiness": marked("READINESS_JSON", {"ready": True, "errors": [], "warnings": [], "raw_counts": True, "cells": 5}),
    "cli_help": f"--help --{PRIVATE.lower()} {PRIVATE}",
    "geo_manifest": f"GSM123  [xenium]  {PRIVATE}\n  files:\n    - {PRIVATE}.rds  (1 MB)",
    "geo_fetch_file": f"fetched: /private/{PRIVATE}.rds",
    "rds_inspect": R_SCHEMA,
    "rds_convert": R_SCHEMA,
    "rds_validate": json.dumps({"cells": 5, "genes": 3, "assays": PRIVATE,
                                "assay_dims": [{"name": PRIVATE, "dims": [3, 5]}],
                                "reduced_dims": PRIVATE, "obs_columns": PRIVATE,
                                "var_columns": PRIVATE, "spatial_dims": [5, 2], "input": PRIVATE}),
    "ingest_spatial": marked("INGEST_SPATIAL_JSON", {"n_samples": 1, "n_cells": 5, "n_genes": 3,
                            "sample_sizes": [5], "controls_dropped": True, "platforms": {"xenium": 1}}),
    "qc_filter": marked("QC_FILTER_JSON", {"n_before": 6, "n_after": 5, "n_removed": 1}),
    "split_sections": marked("SPLIT_SECTIONS_JSON", {"n_sections": 1, "n_groups": 1,
                             "method": "auto", "key": PRIVATE, "groups": [{"pieces": 1, "sizes": [5]}]}),
    "preview_sections": marked("PREVIEW_SECTIONS_JSON", {"n_groups": 1, "n_panels": 1,
                               "method": "auto", "groups": [{"pieces": 1}]}),
    **{name: PRIVATE for name in ("geo_build", "run_preprocess", "generate_notebook", "merge_sections",
                                  "run_companion", "run_export", "package_sidecar", "validate_output")},
}


def test_privacy_fixtures_cover_exactly_the_registered_tools():
    assert set(SUCCESS) == set(tools.TOOL_NAMES)
    assert not any("history" in name for name in tools.TOOL_NAMES)


@pytest.mark.parametrize("name", sorted(SUCCESS))
@pytest.mark.parametrize("backend", ["shared_mcp", "codex"])
@pytest.mark.parametrize("mode", ["success", "error", "exception"])
def test_every_tool_withholds_private_data_through_transport(tmp_path, name, backend, mode):
    async def exercise():
        boundary = Boundary()
        source = tmp_path / (PRIVATE + ".h5ad")
        source.write_bytes(b"synthetic contents")
        alias = boundary.register_path(str(source))
        boundary.output_root = tmp_path / "outputs"
        arguments = {"input_path": alias, "output": "/karo/output/result.h5ad", "paths": [alias]}

        async def handler(local):
            assert local["input_path"] == str(source)
            if mode == "exception":
                raise ValueError(f"Invalid {PRIVATE}: /private/{PRIVATE}")
            stdout = SUCCESS[name]
            # JSON-only formats must stay valid to test their successful path.
            if not name.startswith("rds_"):
                stdout = PRIVATE + "\n" + stdout + "\n" + PRIVATE
            return {"_local": {"stdout": stdout, "stderr": PRIVATE,
                               "returncode": 2 if mode == "error" else 0},
                    "_history": {"path": PRIVATE},
                    "content": [{"type": "text", "text": PRIVATE},
                                {"type": "image", "data": RAW_IMAGE, "mimeType": "image/png"}]}

        if backend == "shared_mcp":
            response = await boundary.invoke(SimpleNamespace(name=name, handler=handler), arguments)
            success = not response.get("is_error")
        else:
            session = codex.Session(on_event=lambda *_: None)
            session.boundary = boundary
            session._thread, session._turn = "thread", "turn"
            sent = []
            async def worker(name, local):
                return await handler(local)
            async def write(message):
                sent.append(message)
            session._run_tool, session._write = worker, write
            await session._tool_call("call", {"threadId": "thread", "turnId": "turn",
                                              "tool": name, "arguments": arguments})
            response, = sent
            success = response["result"]["success"]
        serialized = json.dumps(response)
        assert PRIVATE not in serialized and RAW_IMAGE not in serialized
        assert str(tmp_path) not in serialized and "_history" not in serialized
        assert success == (mode == "success")
        record, = boundary.history.recent()
        assert record["arguments"]["input_path"] == str(source)
        assert record["status"] == ("completed" if mode == "success" else "error")
    asyncio.run(exercise())


@pytest.mark.parametrize("name", ["inspect_input", "inspect_structure", "rds_inspect", "rds_convert",
                                  "rds_validate", "geo_manifest", "geo_fetch_file", "split_sections",
                                  "preview_sections", "ingest_spatial", "qc_filter"])
def test_unrecognized_success_schemas_cannot_release_raw_content(name):
    response = Boundary().filter(name, {}, {}, {"_local": {"returncode": 0, "stdout": PRIVATE}})
    assert response["is_error"]
    assert PRIVATE not in json.dumps(response)
