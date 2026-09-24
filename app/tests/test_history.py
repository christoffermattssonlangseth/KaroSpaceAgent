"""Synthetic runs exercise durable local provenance without any model or data."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from karospace_agent import commands, codex, history, web
from karospace_agent.privacy import Boundary


PRIVATE = "PRIVATE_SUBJECT_19700101"


def report(code=0):
    return {"_local": {"returncode": code, "stdout": "PRIVATE_RAW_VALUES",
                       "stderr": "PRIVATE_RAW_ERRORS"},
            "content": [{"type": "text", "text": "PRIVATE_RAW_CONTENT"}]}


def test_local_history_survives_new_session_and_keeps_private_parameters(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "PRIVATE_CREDENTIAL")
    src = tmp_path / (PRIVATE + ".h5ad")
    src.write_bytes(b"PRIVATE_DATA_CONTENTS")
    out = tmp_path / "filtered.h5ad"
    boundary = Boundary(allow_local_paths=True, provider="claude", model="test-model")

    async def handler(arguments):
        assert arguments["input_path"] == str(src)
        out.write_bytes(b"PRIVATE_DATA_CONTENTS")
        return report()

    response = asyncio.run(boundary.invoke(SimpleNamespace(name="run_preprocess", handler=handler),
                                          {"input_path": str(src), "output": str(out), "resolution": 0.5}))
    assert PRIVATE not in json.dumps(response)
    assert "history" not in json.dumps(response)
    record, = history.RunHistory().recent()
    assert record["status"] == "completed" and record["provider"] == "claude"
    assert record["model"] == "test-model" and record["arguments"]["resolution"] == 0.5
    assert record["inputs"][0]["path"] == str(src)
    assert record["outputs"][0]["size_bytes"] == out.stat().st_size
    assert record["environment"]["python"]
    serialized = json.dumps(record)
    assert PRIVATE in serialized
    for secret in ("PRIVATE_DATA_CONTENTS", "PRIVATE_RAW_VALUES", "PRIVATE_RAW_ERRORS",
                   "PRIVATE_RAW_CONTENT", "PRIVATE_CREDENTIAL"):
        assert secret not in serialized
    if os.name == "posix":
        assert boundary.history.root.stat().st_mode & 0o777 == 0o700
        assert next(boundary.history.root.glob("*/*.json")).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", ["exit", "exception", "cancel"])
def test_failed_and_interrupted_runs_keep_status_without_raw_errors(failure):
    boundary = Boundary()

    async def handler(_):
        if failure == "exit":
            return report(2)
        if failure == "exception":
            raise RuntimeError(PRIVATE)
        raise asyncio.CancelledError()

    task = boundary.invoke(SimpleNamespace(name="run_export", handler=handler), {})
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(task)
    else:
        assert asyncio.run(task)["is_error"]
    record, = boundary.history.recent()
    assert record["status"] == ("interrupted" if failure == "cancel" else "error")
    assert PRIVATE not in json.dumps(record)


def test_unfinished_record_remains_after_restart():
    history.RunHistory().begin("run_export", {})
    record, = history.RunHistory().recent()
    assert record["status"] == "started" and "finished_at" not in record


def test_history_start_failure_prevents_unrecorded_execution(monkeypatch):
    boundary = Boundary()
    def fail(*_):
        raise OSError(PRIVATE)
    monkeypatch.setattr(boundary.history, "begin", fail)
    async def handler(_):
        pytest.fail("tool ran without durable start record")
    response = asyncio.run(boundary.invoke(SimpleNamespace(name="run_export", handler=handler), {}))
    assert json.loads(response["content"][0]["text"])["diagnostic"] == "local_history_unavailable"
    assert PRIVATE not in json.dumps(response)


def test_finish_failure_preserves_tool_result_and_reports_locally(monkeypatch):
    boundary = Boundary()
    write = boundary.history._write
    def fail_finish(record):
        if record["status"] != "started":
            raise OSError(PRIVATE)
        write(record)
    monkeypatch.setattr(boundary.history, "_write", fail_finish)
    async def handler(_):
        return report()
    response = asyncio.run(boundary.invoke(SimpleNamespace(name="run_export", handler=handler), {}))
    assert not response["is_error"] and boundary.history.error
    assert PRIVATE not in json.dumps(response)
    assert boundary.history.recent()[0]["status"] == "started"


def test_command_details_are_recorded_before_subprocess_launch(monkeypatch):
    boundary = Boundary()
    def fake_run(argv, timeout, sink):
        record, = boundary.history.recent()
        assert record["status"] == "started"
        assert record["commands"][0]["argv"] == argv
        return commands.RunResult(0, "", "")
    monkeypatch.setattr(commands, "_run_piped", fake_run)
    async def handler(_):
        commands.run([sys.executable, str(commands.QC_FILTER_SCRIPT), PRIVATE], stream=False)
        return report()
    response = asyncio.run(boundary.invoke(SimpleNamespace(name="qc_filter", handler=handler), {}))
    # The synthetic report has no QC summary, which is correctly rejected.
    assert response["is_error"] and PRIVATE not in json.dumps(response)
    command = boundary.history.recent()[0]["commands"][0]
    assert command["executable"]["exists"]
    assert len(command["scripts"][0]["sha256"]) == 64


def test_codex_worker_command_metadata_stays_local(monkeypatch):
    async def exercise():
        session = codex.Session(on_event=lambda *_: None)
        session._thread, session._turn = "thread", "turn"
        messages = []
        async def worker(name, arguments):
            history.accept_command({"argv": [PRIVATE], "cwd": PRIVATE})
            return report()
        async def write(message):
            messages.append(message)
        session._run_tool, session._write = worker, write
        await session._tool_call("call", {"threadId": "thread", "turnId": "turn",
                                          "tool": "run_export", "arguments": {}})
        record, = session.boundary.history.recent()
        assert record["commands"][0]["argv"] == [PRIVATE]
        assert record["provider"] == "codex"
        assert PRIVATE not in json.dumps(messages)
    asyncio.run(exercise())


def test_history_skips_corrupt_records_and_symlinks(tmp_path):
    store = history.RunHistory()
    record = store.begin("run_export", {})
    folder = store.root / store.session_id
    (folder / "corrupt.json").write_text("not JSON")
    external = tmp_path / "external.json"
    external.write_text(json.dumps({"version": 1, "arguments": PRIVATE}))
    (folder / "linked.json").symlink_to(external)
    assert store.recent() == [record]


def test_history_route_is_local_and_not_part_of_transcript():
    from starlette.testclient import TestClient
    from test_web import FakeSession

    app = web.create_app(session_factory=FakeSession)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 123)) as client:
        store = app.state.conversation._session.boundary.history
        store.begin("run_export", {"input_path": "/private/" + PRIVATE})
        response = client.get("/history", headers={"X-KaroSpace-Local": "1"})
        assert response.status_code == 200 and PRIVATE in response.text
        assert response.headers["cache-control"] == "no-store"
        assert PRIVATE not in json.dumps(app.state.conversation.hub.events)
        assert client.get("/history").status_code == 403
        assert client.get("/history", headers={"X-KaroSpace-Local": "1", "Host": "evil.example"}).status_code == 403
        assert client.get("/history", headers={"X-KaroSpace-Local": "1", "Sec-Fetch-Site": "cross-site"}).status_code == 403
    remote_app = web.create_app(session_factory=FakeSession)
    with TestClient(remote_app, base_url="http://127.0.0.1", client=("192.0.2.1", 123)) as client:
        assert client.get("/history", headers={"X-KaroSpace-Local": "1"}).status_code == 403
