"""`karospace-agent app` — the same local web front end in a native window.

This is the packaged-app face of `web.py`. It runs the identical Starlette app
(same `Session`, same data boundary, same restyled page) on a random localhost
port on a background thread, then shows it in a native window via pywebview
(macOS: the system WKWebView). No browser, no terminal, no visible port.

Why a thread + main-thread window: GUI toolkits require the event loop that owns
the window to run on the main thread, so uvicorn goes to a worker thread and
`webview.start()` keeps the main thread. When the window closes we ask the
server to exit and join it.

The data boundary is unchanged from `web.py`: the tools run locally, only
sanitized metadata reaches the model, and the composer note still warns that
typed text goes to Anthropic as-is.
"""

from __future__ import annotations

import socket
import threading
import time
from importlib import resources

from . import agent

WINDOW_TITLE = "KaroSpace Agent"
_STARTUP_TIMEOUT = 20.0  # seconds to wait for uvicorn before giving up


def _load_icon_bytes() -> bytes:
    """The packaged Dock/app-icon PNG, or b'' if it's somehow missing."""
    try:
        return resources.files(__package__).joinpath("static/appicon.png").read_bytes()
    except Exception:  # pragma: no cover - only if package data is stripped
        return b""


def _set_macos_dock_icon(icon_bytes: bytes) -> None:
    """Replace the generic Dock icon with our mark. Best-effort and macOS-only.

    A pywebview app run from source (not a bundled .app) has no Info.plist icon,
    so macOS shows a blank document in the Dock. AppKit lets us set it live from
    the in-memory PNG. No-op (silently) on other platforms or without pyobjc."""
    if not icon_bytes:
        return
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSData

        data = NSData.dataWithBytes_length_(icon_bytes, len(icon_bytes))
        image = NSImage.alloc().initWithData_(data)
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:  # pragma: no cover - non-macOS or pyobjc absent
        pass


def _free_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for an unused port, then release it for uvicorn to rebind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def run_app(
    model: str = agent.DEFAULT_MODEL,
    opening_message: str | None = None,
    host: str = "127.0.0.1",
    title: str = WINDOW_TITLE,
) -> None:
    """Serve the app on a background thread and show it in a native window.

    Blocks until the window is closed. Raises ImportError with install guidance
    if the desktop or web extras are missing."""
    try:
        import webview
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "The desktop app needs the 'app' extra: pip install 'karospace-agent[app]'"
        ) from e
    try:
        import uvicorn
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "The desktop app needs the 'web' extra: pip install 'karospace-agent[web]'"
        ) from e

    from .web import create_app

    port = _free_port(host)
    app = create_app(model=model, opening_message=opening_message)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, name="karospace-agent-uvicorn", daemon=True)
    thread.start()

    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            server.should_exit = True
            raise RuntimeError("the local server did not start in time")
        time.sleep(0.05)

    webview.create_window(title, url=f"http://{host}:{port}/", width=1100, height=780)
    icon_bytes = _load_icon_bytes()
    try:
        # webview.start(func) runs func once the GUI loop is up — the point at
        # which the NSApplication exists and the Dock icon can be replaced.
        webview.start(lambda: _set_macos_dock_icon(icon_bytes))
    finally:
        server.should_exit = True
        thread.join(timeout=5)
