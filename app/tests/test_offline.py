"""Offline launch routing, credential isolation and the restricted local loop."""
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from karospace_agent import cli, commands, offline, offline_session, tools


def test_long_home_directory_does_not_exceed_unix_socket_limit(tmp_path, monkeypatch):
    home = tmp_path / ("synthetic-long-home-" * 5)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(offline.isolation, "isolated_command", lambda argv: argv)
    monkeypatch.setattr(offline, "model_path", lambda _: tmp_path / "model")
    calls = []

    @contextmanager
    def listener(directory):
        socket_path = str(Path(directory) / "probe.sock")
        assert len(socket_path.encode()) < 104
        assert not Path(directory).is_relative_to(home)
        yield

    def popen(argv, **kwargs):
        configuration = json.loads(Path(argv[-1]).read_text())
        assert len(str(Path(configuration["workspace"]) / "tmp/probe.sock").encode()) >= 104
        probe = configuration["probe_directory"]
        assert f'(subpath "{probe}")' in argv[2]
        assert "(deny network*)" in argv[2]
        calls.append(configuration)
        return SimpleNamespace(wait=lambda: 0, pid=123456789)

    monkeypatch.setattr(offline.isolation, "_unix_listener", listener)
    monkeypatch.setattr(offline.subprocess, "Popen", popen)
    monkeypatch.setattr(offline.os, "killpg", lambda *args: (_ for _ in ()).throw(ProcessLookupError()))
    assert offline.launch(surface="app") == 0
    assert len(calls) == 1
    assert not Path(calls[0]["probe_directory"]).exists()


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
    assert session.send("Describe add_umap") == "Ready."
    assert '"properties"' not in session.messages[0]["content"]
    assert "representation" in model.requests[1]
    assert "tool_unavailable_offline" in model.requests[-1]


@pytest.mark.parametrize("command", ["/help", "/tools", "what can you do?", "can you make a karospace viewer"])
def test_offline_capabilities_do_not_depend_on_model_guessing(tmp_path, command):
    model = Model([])
    events = []
    session = offline_session.OfflineSession(config(tmp_path), model_factory=lambda _: model, on_event=events.append)
    answer = session.send(command)
    assert "KaroSpace viewer" in answer
    assert "network access blocked" in answer
    assert not model.requests
    assert events == [answer]
    if command == "/tools":
        assert "run_export" in answer
        assert "geo_build" not in answer


def test_offline_inspect_uses_selected_path_and_filters_before_model_context(tmp_path, monkeypatch):
    model = Model([])
    calls = []
    selected = tmp_path / "selected.h5ad"
    selected.touch()
    async def inspect(args):
        calls.append(args)
        return {"_local": {"returncode": 0, "stdout": "Cells: 3\nAvailable cell metadata (adata.obs):\n  - PRIVATE_COLUMN [categorical; 2 values; 0 missing] examples: PRIVATE_VALUE\n"}}
    async def structure(args):
        calls.append(args)
        return {"_local": {"returncode": 0, "stdout": "X: dtype=float32, format=dense, all_integer=yes\nobsm: (none)\nspatial_graph_present: no"}}
    monkeypatch.setattr(tools.inspect_input, "handler", inspect)
    monkeypatch.setattr(tools.inspect_structure, "handler", structure)
    session = offline_session.OfflineSession(config(tmp_path) | {"input_path": str(selected)}, model_factory=lambda _: model, on_event=lambda _: None)
    # Follow the web preview's aliasing, including slash-command path aliases.
    draft = session.boundary.preview("/inspect")
    prepared = session.boundary.consume(session.boundary.approve(draft["draft_id"]))
    answer = session.send(prepared)
    assert len(calls) == 2 and all(call["input_path"] == str(selected) for call in calls)
    assert "PRIVATE_VALUE" not in answer
    assert "PRIVATE_COLUMN" not in json.dumps(session.messages)
    assert not model.requests


def test_free_chat_seeds_inspection_before_first_model_turn(tmp_path, monkeypatch):
    selected = tmp_path / "selected.h5ad"
    selected.touch()
    calls = []
    async def inspect(args):
        calls.append(args)
        return {"_local": {"returncode": 0, "stdout": "Cells: 3\nAvailable cell metadata (adata.obs):\n  - some_col [categorical; 2 values; 0 missing]\n"}}
    async def structure(args):
        calls.append(args)
        return {"_local": {"returncode": 0, "stdout": "X: dtype=float32, format=dense, all_integer=yes\nobsm: (none)\nspatial_graph_present: no"}}
    monkeypatch.setattr(tools.inspect_input, "handler", inspect)
    monkeypatch.setattr(tools.inspect_structure, "handler", structure)
    model = Model(['{"message":"Here is the plan."}'])
    session = offline_session.OfflineSession(config(tmp_path) | {"input_path": str(selected)}, model_factory=lambda _: model, on_event=lambda _: None)
    assert session.send("go ahead") == "Here is the plan."
    # Inspection ran once, before the model's only turn, and reached its context.
    assert len(calls) == 2 and all(call["input_path"] == str(selected) for call in calls)
    assert "spatial_graph_present" in model.requests[0]
    # A later free-chat turn reuses the seeded schema instead of re-inspecting.
    model.replies = iter(['{"message":"Still here."}'])
    assert session.send("continue") == "Still here."
    assert len(calls) == 2


def test_free_chat_without_selected_dataset_does_not_inspect(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("no dataset selected; nothing to inspect")
    monkeypatch.setattr(commands, "run", forbidden)
    model = Model(['{"message":"Choose a dataset first."}'])
    session = offline_session.OfflineSession(config(tmp_path), model_factory=lambda _: model, on_event=lambda _: None)
    assert session.send("make a viewer") == "Choose a dataset first."


def test_offline_inspect_without_selected_file_does_not_run_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "run", lambda *a, **kw: pytest.fail("no selected input"))
    session = offline_session.OfflineSession(config(tmp_path), model_factory=lambda _: Model([]), on_event=lambda _: None)
    assert "choose a local" in session.send("/inspect")


def test_selected_dataset_opens_with_reliable_inspection():
    from karospace_agent.offline_ui import opening
    assert opening({"input_path": "/synthetic/input.h5ad", "intent": ""}) == "/inspect"
    assert "Use these options" in opening({"input_path": "/synthetic/input.h5ad", "intent": "Use these options"})


def test_offline_inspect_stops_after_failed_tool(tmp_path, monkeypatch):
    selected = tmp_path / "selected.h5ad"
    selected.touch()
    async def failed(args):
        return {"_local": {"returncode": 1, "stderr": "PRIVATE_FAILURE"}}
    async def forbidden(args):
        pytest.fail("second inspection must not run")
    monkeypatch.setattr(tools.inspect_input, "handler", failed)
    monkeypatch.setattr(tools.inspect_structure, "handler", forbidden)
    session = offline_session.OfflineSession(config(tmp_path) | {"input_path": str(selected)}, model_factory=lambda _: Model([]), on_event=lambda _: None)
    answer = session.send("/inspect")
    assert "could not complete" in answer and "PRIVATE_FAILURE" not in answer


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
