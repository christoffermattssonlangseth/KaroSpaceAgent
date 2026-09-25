"""Registered handlers must enforce fresh local checks, including worker calls."""
import asyncio
import json

import pytest

from karospace_agent import commands, preflight, tools
from karospace_agent.privacy import Boundary


def report(ready=True, errors=None, warnings=None):
    return commands.RunResult(0, "READINESS_JSON " + json.dumps({
        "ready": ready, "errors": errors or [], "warnings": warnings or []}), "")


def arguments(name):
    if name == "run_companion":
        return {"args": ["prepare", "input.h5ad", "--output", "out.h5ad"]}
    return {"input_path": "input.h5ad", "output": "out.h5ad", "flags": []}


@pytest.mark.parametrize("name", sorted(preflight.GUARDED_TOOLS))
def test_all_registered_handlers_block_without_running_processing(monkeypatch, name):
    monkeypatch.setattr(commands, "run_readiness", lambda *a, **kw:
                        report(False, ["invalid_matrix"]))
    def forbidden(*a, **kw):
        pytest.fail("processing ran despite failed readiness")
    for function in ("run_qc_filter", "run_preprocess", "run_add_umap", "run_split_sections",
                     "run_preview_sections", "run_companion", "run_karospace"):
        monkeypatch.setattr(commands, function, forbidden)
    definition = next(t for t in tools.ALL_TOOLS if t.name == name)
    raw = asyncio.run(definition.handler(arguments(name)))
    filtered = Boundary().filter(name, {}, {}, raw)
    assert filtered["is_error"]
    payload = json.loads(filtered["content"][0]["text"])
    assert payload["diagnostic"] == "readiness_blocked"
    assert payload["errors"] == ["invalid_matrix"]


@pytest.mark.parametrize("name", sorted(preflight.GUARDED_TOOLS))
def test_successful_check_uses_operation_requirements(monkeypatch, name):
    calls = []
    monkeypatch.setattr(commands, "run_readiness", lambda *a, **kw: calls.append((a, kw)) or report())
    assert preflight.check(name, arguments(name)) is None
    assert calls[0][1]["require_spatial"] == (name not in {"qc_filter", "run_preprocess", "add_umap"})
    assert calls[0][1]["require_counts"] == (name == "qc_filter")


def test_export_checks_selected_coordinates_table_and_counts(monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "run_readiness", lambda *a, **kw: calls.append(kw) or report())
    args = arguments("run_export") | {"flags": ["--section-key", "old", "--section-key=new",
        "--spatial-x", "x", "--spatial-y=y", "--spatial-key=custom", "--spatialdata-table=table",
        "--statistics-counts-layer", "counts"]}
    assert preflight.check("run_export", args) is None
    assert calls[0] == {"require_spatial": True, "require_counts": True, "section_key": "new",
                        "spatial_x": "x", "spatial_y": "y", "coords_key": "custom",
                        "table": "table", "counts_layer": "counts", "coordinate_mode": "export"}


@pytest.mark.parametrize("result", [commands.RunResult(1, "PRIVATE", "PRIVATE"),
    commands.RunResult(0, 'READINESS_JSON {"ready":true,"errors":[],"warnings":[]}', "", timed_out=True),
    commands.RunResult(0, "not JSON PRIVATE", ""),
    commands.RunResult(0, 'READINESS_JSON {"ready":true,"errors":["PRIVATE"],"warnings":[]}', "")])
def test_unavailable_or_malformed_check_fails_closed(monkeypatch, result):
    monkeypatch.setattr(commands, "run_readiness", lambda *a, **kw: result)
    blocked = preflight.check("qc_filter", arguments("qc_filter"))
    assert blocked.stderr == "readiness_check_unavailable" and "PRIVATE" not in blocked.stdout


@pytest.mark.parametrize("name,args,code", [
    ("run_export", {"flags": ["--output=elsewhere"]}, "readiness_conflicting_output"),
    ("run_export", {"flags": ["-oelsewhere"]}, "readiness_conflicting_output"),
    ("run_export", {"flags": ["--out=elsewhere"]}, "readiness_arguments_unsupported"),
    ("run_export", {"flags": ["--section-k=other"]}, "readiness_arguments_unsupported"),
    ("run_companion", {"args": ["prepare", "in.h5ad", "--output", "out.h5ad", "-oelsewhere"]}, "readiness_conflicting_output"),
    ("run_companion", {"args": ["prepare", "in.h5ad"]}, "readiness_explicit_output_required"),
    ("run_companion", {"args": ["other", "in.h5ad"]}, "readiness_arguments_unsupported"),
])
def test_ambiguous_invocations_block_before_probe(monkeypatch, name, args, code):
    monkeypatch.setattr(commands, "run_readiness", lambda *a, **kw: pytest.fail("should reject arguments first"))
    assert preflight.check(name, arguments(name) | args).stderr == code


def test_every_invocation_rechecks_changed_input(monkeypatch):
    results = iter([report(), report(False, ["nonfinite_expression"])])
    monkeypatch.setattr(commands, "run_readiness", lambda *a, **kw: next(results))
    assert preflight.check("qc_filter", arguments("qc_filter")) is None
    assert preflight.check("qc_filter", arguments("qc_filter")).stderr == "readiness_blocked"


def test_export_checks_default_section_but_allows_explicit_single_section(monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "run_readiness", lambda *a, **kw: calls.append(kw) or report())
    preflight.check("run_export", arguments("run_export"))
    preflight.check("run_export", arguments("run_export") | {"flags": ["--section-key", ""]})
    assert [call["section_key"] for call in calls] == ["sample_id", ""]


def test_blocked_report_rejects_unapproved_diagnostics():
    raw = {"_local": {"returncode": 2, "stderr": "readiness_blocked", "stdout":
        'READINESS_JSON {"ready":false,"errors":["PRIVATE"],"warnings":[]}'}}
    result = Boundary().filter("run_export", {}, {}, raw)
    assert result["is_error"] and "PRIVATE" not in json.dumps(result)


def test_resource_warnings_remain_advisory_and_local(monkeypatch):
    events = []
    monkeypatch.setattr(commands, "run_readiness", lambda *a, **kw:
                        report(warnings=["memory_estimate_exceeds_available"]))
    monkeypatch.setattr(commands, "get_progress_sink", lambda: lambda *event: events.append(event))
    assert preflight.check("qc_filter", arguments("qc_filter")) is None
    assert "memory_estimate_exceeds_available" in events[0][1]
