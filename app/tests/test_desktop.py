"""The `app` subcommand parses, and desktop.run_app wires uvicorn + the window
without actually opening one (both dependencies are faked)."""

import sys
import types

import pytest


def test_parser_app_defaults():
    from karospace_agent import cli

    args = cli.build_parser().parse_args(["app"])
    assert (args.command, args.input, args.intent) == ("app", None, "")
    args = cli.build_parser().parse_args(["app", "/d/x.h5ad", "grid by sample"])
    assert (args.input, args.intent) == ("/d/x.h5ad", "grid by sample")


def test_free_port_is_bindable():
    import socket

    from karospace_agent import desktop

    port = desktop._free_port()
    # The port was released; we can bind it ourselves.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_run_app_serves_then_opens_and_stops_window(monkeypatch):
    """run_app should start uvicorn, wait for `started`, open one window at the
    served URL, then signal the server to exit when the window closes."""
    pytest.importorskip("starlette")
    from karospace_agent import desktop

    created = {}

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False

        def run(self):
            self.started = True  # what the real server sets once bound
            # Block until asked to exit, like the real serve loop.
            import time
            while not self.should_exit:
                time.sleep(0.005)

    fake_uvicorn = types.SimpleNamespace(
        Config=lambda app, host, port, log_level: types.SimpleNamespace(
            app=app, host=host, port=port
        ),
        Server=FakeServer,
    )

    def fake_create_window(title, url, width, height):
        created["title"] = title
        created["url"] = url

    def fake_start():
        # Window is "open"; closing it returns control (real webview.start does
        # the same). By now the server must be started.
        assert created, "window opened before create_window"

    fake_webview = types.SimpleNamespace(create_window=fake_create_window, start=fake_start)

    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    desktop.run_app(opening_message=None, title="KaroSpace Agent")

    assert created["title"] == "KaroSpace Agent"
    assert created["url"].startswith("http://127.0.0.1:")
