"""A thin local web front end over `Session` (`karospace-agent web`).

One process, one conversation, one browser tab (or several, they all see the
same transcript). The server must run where the data lives, because the tools
spawn `karospace` on the .h5ad locally; it binds 127.0.0.1 by default and has
no auth, so treat it as a local app with a browser window, not a service.

    GET  /            the page
    GET  /auth        which credential the model runs under (label only)
    GET  /events      Server-Sent Events: transcript replay, then live
    POST /preview     {"text": ...} -> local draft with aliased paths/names
    POST /send        {"draft_id": ...} -> approve and queue that exact draft
    POST /interrupt   stop the running turn

The data boundary is the Session's: built-ins off, allowlisted tool responses.
What the browser shows — model text, tool-call lines, child-process progress —
is the same local-only view the REPL prints. The only new channel is the
textarea, and the page says so.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from importlib import resources
from pathlib import Path

from . import agent, auth

try:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
    from starlette.routing import Route
except ImportError as e:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "The web UI needs the 'web' extra: pip install 'karospace-agent[web]'"
    ) from e

HEARTBEAT_SECONDS = 15
MAX_PROGRESS_EVENTS = 5_000  # keep the replay bounded on very chatty exports

# The section-preview render script prints one of these per panel on its stdout.
# It rides the LOCAL progress channel (never the model boundary); the front end
# reads the referenced PNG off local disk and shows it. The marker carries a
# local path + aggregate meta only — no coordinate, no group value.
PREVIEW_IMG_MARKER = "KAROSPACE_PREVIEW_IMG "
MAX_PREVIEW_BYTES = 8 * 1024 * 1024  # skip an implausibly large "panel"


class Hub:
    """The transcript as an append-only event log, fanned out to SSE readers.

    Every event gets a sequential `id`, so a reconnecting browser can resume
    from `Last-Event-ID` and a fresh one replays everything. Progress lines are
    capped: beyond MAX_PROGRESS_EVENTS the oldest are dropped from the replay
    (they were already shown live)."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._next_id = 0
        self._progress_count = 0
        self._subscribers: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def publish(self, event: dict) -> None:
        event = {"id": self._next_id, **event}
        self._next_id += 1
        self.events.append(event)
        if event["type"] == "progress":
            self._progress_count += 1
            if self._progress_count > MAX_PROGRESS_EVENTS:
                self._drop_oldest_progress()
        for q in self._subscribers:
            q.put_nowait(event)

    def publish_threadsafe(self, event: dict) -> None:
        """For callers on other threads (the subprocess pump threads)."""
        assert self.loop is not None, "Hub.loop must be set before threads publish"
        self.loop.call_soon_threadsafe(self.publish, event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def since(self, last_id: int | None) -> list[dict]:
        if last_id is None:
            return list(self.events)
        return [e for e in self.events if e["id"] > last_id]

    def _drop_oldest_progress(self) -> None:
        for i, e in enumerate(self.events):
            if e["type"] == "progress":
                del self.events[i]
                self._progress_count -= 1
                return


class Conversation:
    """Owns the Session and runs user turns one at a time off a queue."""

    def __init__(self, hub: Hub, session_factory: Callable[..., object]) -> None:
        self.hub = hub
        self._session_factory = session_factory
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._session = None
        self.state = "idle"

    async def start(self) -> None:
        self.hub.loop = asyncio.get_running_loop()
        self._session = self._session_factory(
            on_event=self._on_event, on_progress=self._on_progress
        )
        await self._session.__aenter__()
        self._worker = asyncio.create_task(self._run(), name="karospace-agent-turns")
        self._set_state("idle")

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        if self._session is not None:
            await self._session.__aexit__(None, None, None)

    def send(self, text: str) -> int:
        """Queue a user turn; returns how many turns are now waiting."""
        self.hub.publish({"type": "user", "text": text})
        self._queue.put_nowait(text)
        return self._queue.qsize()

    async def interrupt(self) -> None:
        if self.state == "working" and self._session is not None:
            self._set_state("interrupting")
            await self._session.interrupt()

    async def _run(self) -> None:
        while True:
            text = await self._queue.get()
            self._set_state("working")
            try:
                await self._session.send(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - surface, keep serving
                self.hub.publish({"type": "error", "text": f"{type(e).__name__}: {e}"})
            finally:
                self._set_state("idle")

    def _set_state(self, state: str) -> None:
        self.state = state
        self.hub.publish({"type": "status", "state": state})

    # Session callbacks. `_on_event` runs on the loop thread (inside
    # Session.send); `_on_progress` runs on the subprocess pump threads.
    def _on_event(self, kind: str, text: str) -> None:
        if kind == "text":
            self.hub.publish({"type": "assistant", "text": text})
        elif kind == "tool":
            self.hub.publish({"type": "tool", "text": text})

    def _on_progress(self, stream: str, line: str) -> None:
        line = line.rstrip("\n")
        # A section-preview panel: read the local PNG and push it as an image
        # event instead of dumping the marker into the export log. The bytes
        # never touch the model boundary — this is the local channel only.
        if stream == "stdout" and line.startswith(PREVIEW_IMG_MARKER):
            event = self._preview_image_event(line[len(PREVIEW_IMG_MARKER):].strip())
            if event is not None:
                self.hub.publish_threadsafe(event)
            return
        self.hub.publish_threadsafe(
            {"type": "progress", "stream": stream, "line": line}
        )

    def _preview_image_event(self, payload: str) -> dict | None:
        """Turn a preview marker into an inline image event, reading the PNG off
        local disk. Returns None (drop it) if anything looks off. The path is
        confined to this session's output dir, so nothing outside it is served."""
        try:
            meta = json.loads(payload)
            path = Path(meta["path"]).resolve()
            root = Path(self._session.boundary.output_root).resolve()
            if root not in path.parents or path.suffix.lower() != ".png":
                return None
            raw = path.read_bytes()
        except Exception:  # noqa: BLE001 - a bad marker just isn't shown
            return None
        if not raw or len(raw) > MAX_PREVIEW_BYTES:
            return None
        src = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        return {
            "type": "image", "src": src,
            "group": int(meta.get("group", 0)),
            "pieces": int(meta.get("pieces", 0)),
        }


def _sse(event: dict) -> str:
    return f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"


async def event_stream(hub: Hub, last_id: int | None) -> AsyncIterator[str]:
    """Replay from `last_id` (exclusive), then live, with heartbeats."""
    q = hub.subscribe()
    try:
        for e in hub.since(last_id):
            yield _sse(e)
        while True:
            try:
                e = await asyncio.wait_for(q.get(), HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield _sse(e)
    finally:
        hub.unsubscribe(q)


def _page() -> str:
    return resources.files(__package__).joinpath("static/index.html").read_text("utf-8")


def create_app(
    model: str | None = None,
    opening_message: str | None = None,
    session_factory: Callable[..., object] | None = None,
    provider: str = agent.DEFAULT_PROVIDER,
) -> Starlette:
    """Build the app. `session_factory` is injectable for tests; the default
    makes a real `agent.Session` for `model`."""
    hub = Hub()
    factory = session_factory or (lambda **kw: agent.create_session(provider, model, **kw))
    convo = Conversation(hub, factory)

    async def index(request: Request):
        return HTMLResponse(_page())

    async def auth_status(request: Request):
        status = await asyncio.to_thread(agent.auth_status, provider)
        return JSONResponse(
            {"label": status.label, "detail": status.detail, "ok": status.ok,
             "provider": provider}
        )

    async def events(request: Request):
        raw = request.headers.get("last-event-id") or request.query_params.get("since")
        last_id = int(raw) if raw and raw.isdigit() else None
        return StreamingResponse(
            event_stream(hub, last_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def send(request: Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        draft_id = body.get("draft_id") if isinstance(body, dict) else None
        if not isinstance(draft_id, str):
            return JSONResponse({"error": "A reviewed privacy preview is required."}, status_code=400)
        try:
            message = convo._session.boundary.approve(draft_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"queued": convo.send(message)}, status_code=202)

    async def preview(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            return JSONResponse({"error": "text is required"}, status_code=400)
        return JSONResponse(convo._session.boundary.preview(text.strip()))

    async def opening(request: Request):
        # Local-only draft; opening a window never sends its input automatically.
        return JSONResponse({"text": opening_message or ""})

    async def interrupt(request: Request):
        await convo.interrupt()
        return JSONResponse({"state": convo.state}, status_code=202)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        await convo.start()
        try:
            yield
        finally:
            await convo.stop()

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/auth", auth_status),
            Route("/events", events),
            Route("/send", send, methods=["POST"]),
            Route("/preview", preview, methods=["POST"]),
            Route("/opening", opening),
            Route("/interrupt", interrupt, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.conversation = convo  # for tests / introspection
    return app


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    model: str | None = None,
    opening_message: str | None = None,
    provider: str = agent.DEFAULT_PROVIDER,
) -> None:
    import uvicorn

    uvicorn.run(
        create_app(model=model, opening_message=opening_message, provider=provider),
        host=host,
        port=port,
        log_level="warning",
    )
