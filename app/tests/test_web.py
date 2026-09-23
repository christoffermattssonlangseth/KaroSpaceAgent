"""The web front end: event log/replay, the turn worker, and the HTTP routes.
A fake Session stands in for the SDK; no model runs."""

import asyncio
import json

import pytest

pytest.importorskip("claude_agent_sdk")
pytest.importorskip("starlette")

from karospace_agent import web
from karospace_agent.privacy import Boundary  # noqa: E402


class FakeSession:
    """Mimics agent.Session: emits one tool call, one progress line (from a
    thread, like the real pump), and one text block per turn."""

    instances = []

    def __init__(self, on_event, on_progress):
        self.on_event = on_event
        self.on_progress = on_progress
        self.boundary = Boundary()
        self.sent = []
        self.interrupted = 0
        self.entered = self.exited = False
        self.block = None  # set to an Event to make send() wait on it
        FakeSession.instances.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True

    async def send(self, text):
        self.sent.append(text)
        self.on_event("tool", "inspect_input({'input_path': 'x.h5ad'})")
        await asyncio.to_thread(self.on_progress, "stdout", "exporting 1/3\n")
        await asyncio.sleep(0)  # let the threadsafe publish land
        if self.block is not None:
            await self.block.wait()
        self.on_event("text", f"done: {text}")
        self.on_event("result", f"done: {text}")
        return f"done: {text}"

    async def interrupt(self):
        self.interrupted += 1
        if self.block is not None:
            self.block.set()


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeSession.instances.clear()


# --- Hub -------------------------------------------------------------------

def test_hub_assigns_ids_and_replays_since():
    async def main():
        hub = web.Hub()
        hub.loop = asyncio.get_running_loop()
        hub.publish({"type": "user", "text": "a"})
        hub.publish({"type": "assistant", "text": "b"})
        hub.publish_threadsafe({"type": "progress", "stream": "stdout", "line": "c"})
        await asyncio.sleep(0)
        assert [e["id"] for e in hub.events] == [0, 1, 2]
        assert [e["type"] for e in hub.since(None)] == ["user", "assistant", "progress"]
        assert [e["id"] for e in hub.since(0)] == [1, 2]
        assert hub.since(2) == []

    asyncio.run(main())


def test_hub_caps_progress_in_replay(monkeypatch):
    monkeypatch.setattr(web, "MAX_PROGRESS_EVENTS", 3)
    hub = web.Hub()
    hub.publish({"type": "user", "text": "keep me"})
    for i in range(5):
        hub.publish({"type": "progress", "stream": "stdout", "line": str(i)})
    kept = [e["line"] for e in hub.events if e["type"] == "progress"]
    assert kept == ["2", "3", "4"]
    assert hub.events[0]["type"] == "user"  # non-progress never dropped


def test_event_stream_replays_then_streams_live():
    async def main():
        hub = web.Hub()
        hub.publish({"type": "user", "text": "old"})
        gen = web.event_stream(hub, None)
        first = await asyncio.wait_for(gen.__anext__(), 1)
        assert first.startswith("id: 0\nevent: user\n")
        assert json.loads(first.split("data: ", 1)[1]) == {"id": 0, "type": "user", "text": "old"}
        hub.publish({"type": "status", "state": "working"})
        live = await asyncio.wait_for(gen.__anext__(), 1)
        assert "event: status" in live
        await gen.aclose()
        assert hub._subscribers == set()

    asyncio.run(main())


def test_event_stream_heartbeats_when_quiet(monkeypatch):
    monkeypatch.setattr(web, "HEARTBEAT_SECONDS", 0.01)

    async def main():
        gen = web.event_stream(web.Hub(), None)
        assert await asyncio.wait_for(gen.__anext__(), 1) == ": ping\n\n"
        await gen.aclose()

    asyncio.run(main())


# --- Conversation ----------------------------------------------------------

def test_conversation_runs_turns_in_order_and_publishes_events():
    async def main():
        hub = web.Hub()
        convo = web.Conversation(hub, FakeSession)
        await convo.start()
        convo.send("first")
        convo.send("second")
        await _until(lambda: convo.state == "idle" and len(FakeSession.instances[0].sent) == 2)
        await convo.stop()

        s = FakeSession.instances[0]
        assert s.entered and s.exited
        assert s.sent == ["first", "second"]
        types = [e["type"] for e in hub.events]
        assert types.count("user") == 2
        assert types.count("tool") == 2
        assert types.count("progress") == 2
        assert types.count("assistant") == 2
        # user event precedes that turn's outputs
        assert types.index("user") < types.index("tool")
        assert hub.events[-1] == {"id": hub.events[-1]["id"], "type": "status", "state": "idle"}

    asyncio.run(main())


def test_conversation_interrupt_only_while_working():
    async def main():
        hub = web.Hub()
        convo = web.Conversation(hub, FakeSession)
        await convo.start()
        s = FakeSession.instances[0]
        await convo.interrupt()  # idle: no-op
        assert s.interrupted == 0

        s.block = asyncio.Event()
        convo.send("slow")
        await _until(lambda: convo.state == "working" and s.sent == ["slow"])
        await convo.interrupt()
        assert s.interrupted == 1
        await _until(lambda: convo.state == "idle")
        states = [e["state"] for e in hub.events if e["type"] == "status"]
        assert states[-3:] == ["working", "interrupting", "idle"]
        await convo.stop()

    asyncio.run(main())


def test_conversation_survives_a_failing_turn():
    class Boom(FakeSession):
        async def send(self, text):
            raise RuntimeError("transport died")

    async def main():
        hub = web.Hub()
        convo = web.Conversation(hub, Boom)
        await convo.start()
        convo.send("x")
        await _until(lambda: any(e["type"] == "error" for e in hub.events))
        assert "RuntimeError: transport died" in [e.get("text") for e in hub.events]
        await _until(lambda: convo.state == "idle")
        convo.send("y")  # still accepting turns
        await _until(lambda: len(FakeSession.instances[0].sent) == 0 and convo._queue.empty())
        await convo.stop()

    asyncio.run(main())


# Stand-in PNG bytes: the front end only reads the file and base64-encodes it,
# so the PNG magic header plus any payload is enough to exercise the path.
_PNG_1X1 = b"\x89PNG\r\n\x1a\n" + b"local-preview-panel-bytes"


def test_conversation_shows_a_local_preview_as_an_image_event(tmp_path):
    async def main():
        hub = web.Hub()
        convo = web.Conversation(hub, FakeSession)
        await convo.start()
        session = FakeSession.instances[0]
        root = tmp_path / "session_out"
        root.mkdir()
        session.boundary.output_root = root
        panel = root / "panel_1.png"
        panel.write_bytes(_PNG_1X1)

        marker = web.PREVIEW_IMG_MARKER + json.dumps(
            {"path": str(panel), "group": 1, "pieces": 3})
        convo._on_progress("stdout", marker + "\n")
        await asyncio.sleep(0)  # let the threadsafe publish land

        images = [e for e in hub.events if e["type"] == "image"]
        assert len(images) == 1
        assert images[0]["src"].startswith("data:image/png;base64,")
        assert images[0]["group"] == 1 and images[0]["pieces"] == 3
        # The marker line is consumed, not dumped into the export log, and the
        # local path never appears anywhere in the transcript.
        assert not any(e["type"] == "progress" for e in hub.events)
        assert str(panel) not in json.dumps(hub.events)
        await convo.stop()

    asyncio.run(main())


def test_conversation_drops_a_preview_outside_the_session_dir(tmp_path):
    async def main():
        hub = web.Hub()
        convo = web.Conversation(hub, FakeSession)
        await convo.start()
        session = FakeSession.instances[0]
        session.boundary.output_root = tmp_path / "session_out"
        (tmp_path / "session_out").mkdir()
        # A real PNG, but sitting OUTSIDE the session output dir: refuse to serve it.
        outsider = tmp_path / "elsewhere.png"
        outsider.write_bytes(_PNG_1X1)

        marker = web.PREVIEW_IMG_MARKER + json.dumps(
            {"path": str(outsider), "group": 1, "pieces": 2})
        convo._on_progress("stdout", marker + "\n")
        await asyncio.sleep(0)

        assert not any(e["type"] == "image" for e in hub.events)
        assert not any(e["type"] == "progress" for e in hub.events)
        await convo.stop()

    asyncio.run(main())


async def _until(pred, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


# --- HTTP ------------------------------------------------------------------

def test_routes_end_to_end():
    from starlette.testclient import TestClient

    app = web.create_app(session_factory=FakeSession, opening_message="open with build")
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200 and "KaroSpace Agent" in r.text
        assert "Review the outgoing text" in r.text  # the boundary note is on the page

        assert client.post("/send", json={"text": "  "}).status_code == 400
        assert client.post("/send", content=b"nope").status_code == 400
        assert FakeSession.instances[0].sent == []
        assert client.post("/send", json={"text": "hello"}).status_code == 400
        opening = client.get("/opening").json()["text"]
        for message in (opening, "hello"):
            draft = client.post("/preview", json={"text": message}).json()
            assert draft["text"] == message
            r = client.post("/send", json={"draft_id": draft["draft_id"]})
            assert r.status_code == 202
            assert client.post("/send", json={"draft_id": draft["draft_id"]}).status_code == 400
        assert r.status_code == 202 and "queued" in r.json()

        r = client.post("/interrupt")
        assert r.status_code == 202 and r.json()["state"] in {"idle", "working", "interrupting"}

        convo = app.state.conversation
        # Both the opening build and the user's message reach the session.
        import time
        for _ in range(200):
            if FakeSession.instances and FakeSession.instances[0].sent == ["open with build", "hello"]:
                break
            time.sleep(0.01)
        assert FakeSession.instances[0].sent == ["open with build", "hello"]
        texts = [e["text"] for e in convo.hub.events if e["type"] == "user"]
        assert texts == ["open with build", "hello"]
    assert FakeSession.instances[0].exited  # lifespan shutdown closed the session


def test_parser_web_defaults_to_localhost():
    from karospace_agent import cli

    args = cli.build_parser().parse_args(["web"])
    assert (args.host, args.port, args.input) == ("127.0.0.1", 8765, None)
    args = cli.build_parser().parse_args(["web", "/d/x.h5ad", "grid", "--port", "9000"])
    assert (args.input, args.intent, args.port) == ("/d/x.h5ad", "grid", 9000)


def test_auth_route_reports_label_not_secrets(monkeypatch):
    from starlette.testclient import TestClient
    from karospace_agent import auth

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret")
    # Isolate the API-key path: clear any ambient cloud-provider selectors
    # (Bedrock/Vertex/Foundry) that outrank it, so this passes regardless of the
    # host environment (e.g. a session running under CLAUDE_CODE_USE_FOUNDRY).
    for var in auth.CLOUD_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    app = web.create_app(session_factory=FakeSession)
    with TestClient(app) as client:
        r = client.get("/auth")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "API key" in body["label"]
    assert "very-secret" not in r.text
