"""Offline launch routing, credential isolation and the restricted local loop."""
import json
from pathlib import Path

import pytest

from karospace_agent import cli, commands, offline, offline_session, tools


def config(tmp_path):
    (tmp_path / "output").mkdir(exist_ok=True)
    return {"workspace": str(tmp_path), "model": "synthetic model"}


class Model:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def complete(self, messages):
        self.requests.append(json.dumps(messages))
        return next(self.replies)


@pytest.mark.parametrize("surface", ["app", "chat"])
def test_offline_route_cannot_authenticate_or_create_a_cloud_session(monkeypatch, surface):
    calls = []
    monkeypatch.setattr(cli, "_preflight", lambda *a: pytest.fail("cloud preflight"))
    monkeypatch.setattr(offline, "launch", lambda **kw: calls.append(kw) or 0)
    assert cli.main([surface, "--offline", "--provider", "codex", "--local-model", "local-model"]) == 0
    assert calls[0]["model"] == "local-model" and calls[0]["surface"] == surface


def test_local_model_requires_offline_mode(monkeypatch):
    monkeypatch.setattr(cli, "_preflight", lambda *a: pytest.fail("cloud preflight"))
    assert cli.main(["app", "--local-model", "local-model"]) == 2


def test_environment_does_not_inherit_credentials_proxies_or_endpoints(monkeypatch, tmp_path):
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HTTPS_PROXY", "OLLAMA_HOST", "PYTHONPATH", "DYLD_INSERT_LIBRARIES"):
        monkeypatch.setenv(key, "PRIVATE_CREDENTIAL")
    monkeypatch.setattr(commands, "karospace_bin", lambda: None)
    monkeypatch.setattr(commands, "companion_bin", lambda: None)
    monkeypatch.setattr(commands, "rds2h5ad_bin", lambda: None)
    env = offline.clean_environment(tmp_path, Path("/python/bin/python"))
    assert "PRIVATE_CREDENTIAL" not in json.dumps(env)
    assert env["HF_HUB_OFFLINE"] == "1" and env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["HOME"] == str(tmp_path / "home")


def test_missing_model_never_becomes_a_repository_download(tmp_path):
    with pytest.raises((OSError, ValueError)):
        offline.model_path(str(tmp_path / "nonexistent-model"))
    (tmp_path / "config.json").write_text("{}")
    with pytest.raises(ValueError, match="weights"):
        offline.model_path(str(tmp_path))


@pytest.mark.parametrize("value", ["plain text", "[]", '{"message": 1}', '{"tool":"x","arguments":{},"extra":1}'])
def test_actions_require_a_strict_envelope(value):
    with pytest.raises((TypeError, ValueError)):
        offline_session.parse_action(value)


def test_unknown_and_online_tools_never_execute(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("no command may run")
    monkeypatch.setattr(commands, "run", forbidden)
    model = Model(['{"tool":"geo_build","arguments":{}}', '{"tool":"bash","arguments":{}}', '{"message":"Unavailable offline."}'])
    session = offline_session.OfflineSession(config(tmp_path), model_factory=lambda _: model, on_event=lambda _: None)
    assert session.send("Use a local dataset") == "Unavailable offline."
    assert all(not name.startswith("geo_") for name in session.specs)
    assert "tool_unavailable_offline" in model.requests[-1]


def test_tool_descriptions_are_disclosed_locally_on_demand(tmp_path):
    model = Model(['{"describe_tool":"add_umap"}', '{"describe_tool":"geo_build"}', '{"message":"Ready."}'])
    session = offline_session.OfflineSession(config(tmp_path), model_factory=lambda _: model, on_event=lambda _: None)
    assert session.send("What can you do?") == "Ready."
    assert '"properties"' not in session.messages[0]["content"]
    assert "representation" in model.requests[1]
    assert "tool_unavailable_offline" in model.requests[-1]


def test_local_tool_results_still_cross_the_privacy_boundary(tmp_path, monkeypatch):
    model = Model(['{"tool":"inspect_structure","arguments":{"input_path":"/karo/output/input.h5ad","spatialdata_table":""}}', '{"message":"Inspected."}'])
    calls = []
    async def raw(args):
        calls.append(args)
        return {"_local": {"returncode": 0, "stdout": "PRIVATE_VALUE\nX: dtype=float32, format=dense, all_integer=yes\nobsm: (none)\nspatial_graph_present: no"}}
    monkeypatch.setattr(tools.inspect_structure, "handler", raw)
    session = offline_session.OfflineSession(config(tmp_path), model_factory=lambda _: model, on_event=lambda _: None)
    session.send("Inspect the local file")
    assert len(calls) == 1
    assert "PRIVATE_VALUE" not in model.requests[-1]
    assert "spatial_graph_present" in model.requests[-1]


def test_schema_validation_stops_invalid_model_arguments(tmp_path, monkeypatch):
    async def forbidden(args):
        pytest.fail("invalid arguments reached a tool")
    monkeypatch.setattr(tools.validate_output, "handler", forbidden)
    model = Model(['{"tool":"validate_output","arguments":{"paths":"not a list"}}', '{"message":"Try again."}'])
    session = offline_session.OfflineSession(config(tmp_path), model_factory=lambda _: model, on_event=lambda _: None)
    session.send("Check outputs")
    assert "invalid_tool_arguments" in model.requests[-1]


def test_failed_current_process_verification_prevents_model_initialization(tmp_path, monkeypatch):
    from karospace_agent import offline_bootstrap
    configuration = tmp_path / "config.json"
    configuration.write_text(json.dumps(config(tmp_path) | {"smoke_test": True}))
    monkeypatch.setattr(offline_bootstrap.sys, "argv", ["bootstrap", str(configuration)])
    monkeypatch.setattr(offline_bootstrap.sys, "path", list(offline_bootstrap.sys.path))
    def reject(_):
        raise RuntimeError("not confined")
    monkeypatch.setattr(offline_bootstrap, "verify", reject)
    monkeypatch.setattr(offline_session, "OfflineSession", lambda *a, **kw: pytest.fail("model loaded before confinement"))
    with pytest.raises(RuntimeError, match="not confined"):
        offline_bootstrap.main()


def test_preview_is_confined_to_session_output_and_bounded(tmp_path):
    import base64
    import struct
    from karospace_agent.offline_ui import local_preview
    output = tmp_path / "output"
    output.mkdir()
    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 1, 1)
    path = output / "preview.png"
    path.write_bytes(header)
    def line(path):
        return "KAROSPACE_PREVIEW_IMG " + json.dumps({"path": str(path)})
    assert base64.b64decode(local_preview(line(path), tmp_path)) == header
    outside = tmp_path / "outside.png"
    outside.write_bytes(header)
    assert local_preview(line(outside), tmp_path) is None
    linked = output / "linked.png"
    linked.symlink_to(outside)
    assert local_preview(line(linked), tmp_path) is None
    path.write_bytes(header[:16] + struct.pack(">II", 100000, 100000))
    assert local_preview(line(path), tmp_path) is None
