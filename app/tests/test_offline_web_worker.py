"""Shared UI requests stay on local pipes and retain the privacy boundary."""
import io
import json

from karospace_agent import offline_web_worker
from karospace_agent.history import RunHistory
from karospace_agent.privacy import Boundary


def test_shared_ui_preview_send_and_history_use_local_boundary(tmp_path, monkeypatch, capsys):
    sessions, calls = [], []

    class Session:
        def __init__(self, config, on_event, on_progress):
            self.boundary = Boundary(history=RunHistory(root=tmp_path / "history"))
            self.reply = on_event
            sessions.append(self)
            print("PRIVATE_DEPENDENCY_LOG")

        def send(self, text):
            calls.append(text)
            print("PRIVATE_TOOL_LOG")
            self.reply("Ready.")

    def requests():
        yield json.dumps({"id": 1, "path": "/auth", "body": {}})
        yield json.dumps({"id": 2, "path": "/preview", "body": {"text": "Inspect /synthetic/private.h5ad"}})
        draft = next(iter(sessions[0].boundary._drafts))
        yield json.dumps({"id": 3, "path": "/send", "body": {"draft_id": draft}})
        yield json.dumps({"id": 4, "path": "/history", "body": {}})

    monkeypatch.setattr(offline_web_worker, "OfflineSession", Session)
    monkeypatch.setattr(offline_web_worker.sys, "stdin", requests())
    offline_web_worker.run({"workspace": str(tmp_path)})
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines()]
    assert events[0] == {"worker_verified": True}
    assert {"type": "assistant", "text": "Ready."} in events
    assert calls == ["Inspect /karo/files/file_1.h5ad"]
    assert "PRIVATE" not in captured.out
    assert "PRIVATE_TOOL_LOG" in captured.err
    assert next(event for event in events if event.get("id") == 4)["body"]["records"] == []


def test_invalid_requests_and_unreviewed_send_never_reach_model(tmp_path, monkeypatch, capsys):
    class Session:
        def __init__(self, *args, **kwargs):
            self.boundary = Boundary(history=RunHistory(root=tmp_path / "history"))

        def send(self, text):
            raise AssertionError("Unreviewed input reached model")

    requests = [{"id": 1, "path": "/send", "body": {"draft_id": "PRIVATE_INVALID"}},
                {"id": 2, "path": "/bash", "body": {}}, {"command": "bash"}]
    monkeypatch.setattr(offline_web_worker, "OfflineSession", Session)
    monkeypatch.setattr(offline_web_worker.sys, "stdin", io.StringIO("\n".join(map(json.dumps, requests))))
    offline_web_worker.run({"workspace": str(tmp_path)})
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines()]
    assert len([event for event in events if event.get("status") == 400]) == 3
    assert "PRIVATE_INVALID" not in captured.out + captured.err
