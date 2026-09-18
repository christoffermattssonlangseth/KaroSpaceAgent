"""`karospace-agent build <input> "<intent>"`, `karospace-agent chat`, and
`karospace-agent web` — the entry points.

`build` composes the user's file + plain-English intent into one message and
hands it to the one-shot agent loop. `chat` opens a multi-turn session: the
same first message if a file is given, then a read-eval loop so the model can
ask questions (single-section trap, downsampling) and act on the answers, and
the user can iterate on a built viewer without starting over.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import threading
from collections.abc import Callable

from . import agent, auth
from .commands import companion_bin, companion_version, karospace_bin

try:  # line editing + history at the `you>` prompt, where available
    import readline  # noqa: F401
except ImportError:  # pragma: no cover - Windows without pyreadline
    pass

QUIT_COMMANDS = {"/quit", "/exit", "/q"}
PROMPT = "\nyou> "

CHAT_BANNER = """\
karospace-agent chat — type a message, /quit to leave (Ctrl-D also works).
Ctrl-C while the agent is working interrupts that turn; twice quits.
Only sanitized schema reaches the model through the tools, but whatever YOU
type here is sent to Anthropic as-is: give file paths and column NAMES, never
sample IDs, coordinates, or other data values."""


def _preflight() -> list[str]:
    """Non-fatal environment notes surfaced before a run."""
    notes = []
    status = auth.detect()
    if not status.ok:
        notes.append(_auth_warning(status))
    if karospace_bin() is None:
        notes.append("karospace not found on PATH (set KAROSPACE_BIN) — required.")
    if companion_bin() is None:
        notes.append(
            "karospace-companion not found — spatial-graph pre-processing (the "
            "default route) unavailable; viewers will build without neighbor tools "
            "(build ../KaroSpaceCompanion or set KAROSPACE_COMPANION)."
        )
    else:
        ver = companion_version()
        # karospace has no --version to compare against, so we can't diff the two;
        # surfacing the companion version at least makes drift visible in the notes.
        notes.append(
            f"karospace-companion {ver} — spatial graph built by default."
            if ver
            else "karospace-companion present — spatial graph built by default."
        )
    return notes


def _auth_warning(status: auth.AuthStatus) -> str:
    if status.source is None:
        return (
            f"no usable Claude credential ({status.detail}). "
            "Run `karospace-agent auth` for how to sign in."
        )
    return (
        f"{status.label}: {status.detail}. "
        "Run `karospace-agent auth` for the permitted options."
    )


def auth_report(status: auth.AuthStatus | None = None) -> tuple[str, int]:
    """Text + exit code for `karospace-agent auth`: 0 usable, 1 none, 3 not permitted."""
    status = status or auth.detect()
    lines = [status.line()]
    if status.ok:
        lines.append("This is a permitted credential for karospace-agent.")
        code = 0
    elif status.source is None:
        lines.append("")
        lines.append(auth.CONSOLE_SIGNIN_HELP)
        code = 1
    else:
        lines.append(
            "NOT PERMITTED for this app: Anthropic's terms reserve claude.ai logins "
            "and subscription tokens for Claude Code and claude.ai themselves, and the "
            "Agent SDK docs say third-party agents must use the API routes instead."
        )
        lines.append("")
        lines.append(auth.CONSOLE_SIGNIN_HELP)
        code = 3
    return "\n".join(lines), code


def _compose_prompt(input_path: str, intent: str) -> str:
    intent = intent.strip() or "Build the most useful viewer for this dataset."
    return (
        f"Build a KaroSpace viewer.\n\n"
        f"Input file: {input_path}\n"
        f"Researcher intent: {intent}\n\n"
        f"Start by inspecting the input, then follow the workflow."
    )


def _read_line(prompt: str = PROMPT) -> str | None:
    """Blocking stdin read; None on EOF. Runs on a worker thread."""
    try:
        return input(prompt)
    except EOFError:
        return None


async def _ainput(read_line: Callable[[], str | None]) -> str | None:
    """Await one line of input without blocking the event loop.

    A *daemon* thread, not `asyncio.to_thread`: the default executor's threads
    are joined at shutdown, so a Ctrl-C at the prompt would hang until the user
    also pressed Enter. A daemon thread just dies with the process."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str | None] = loop.create_future()

    def deliver(fn, value) -> None:
        if not fut.done():
            fn(value)

    def worker() -> None:
        try:
            result = read_line()
        except BaseException as e:  # noqa: BLE001 - hand anything back to the loop
            loop.call_soon_threadsafe(deliver, fut.set_exception, e)
            return
        loop.call_soon_threadsafe(deliver, fut.set_result, result)

    threading.Thread(target=worker, name="karospace-agent-stdin", daemon=True).start()
    return await fut


class TurnInterrupter:
    """SIGINT policy while a turn is running.

    First Ctrl-C asks the model to stop the current turn (`Session.interrupt`),
    which ends the turn normally and returns to the prompt. A second Ctrl-C
    cancels the turn outright, which unwinds the REPL and exits. Outside a turn
    the handler is removed, so Ctrl-C at the prompt is a plain exit."""

    def __init__(self, session: agent.Session) -> None:
        self._session = session
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        self._installed = False
        self._pending: asyncio.Task | None = None
        self.count = 0

    def on_sigint(self) -> None:
        self.count += 1
        if self.count == 1:
            print(
                "\n^C  interrupting this turn (Ctrl-C again to quit)…",
                file=sys.stderr,
                flush=True,
            )
            self._pending = self._loop.create_task(self._session.interrupt())
            self._pending.add_done_callback(_swallow)
        elif self._task is not None:
            self._task.cancel()

    async def __aenter__(self) -> "TurnInterrupter":
        try:
            self._loop.add_signal_handler(signal.SIGINT, self.on_sigint)
            self._installed = True
        except (NotImplementedError, RuntimeError):  # Windows / non-main thread
            self._installed = False
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._installed:
            self._loop.remove_signal_handler(signal.SIGINT)


def _swallow(task: asyncio.Task) -> None:
    """Consume an interrupt task's outcome so a failure is not logged as
    'exception never retrieved' — the turn itself surfaces any error."""
    if not task.cancelled():
        task.exception()


async def _turn(session: agent.Session, text: str) -> str:
    async with TurnInterrupter(session):
        return await session.send(text)


async def chat_loop(
    session: agent.Session,
    first_message: str | None = None,
    read_line: Callable[[], str | None] = _read_line,
) -> None:
    """The REPL: optional opening build request, then user turns until quit/EOF.

    `read_line` is injectable so tests can script a conversation."""
    if first_message:
        await _turn(session, first_message)
    while True:
        line = await _ainput(read_line)
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        if line.lower() in QUIT_COMMANDS:
            break
        await _turn(session, line)


async def _chat(input_path: str | None, intent: str, model: str) -> None:
    first = _compose_prompt(input_path, intent) if input_path else None
    async with agent.Session(model=model) as session:
        await chat_loop(session, first)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="karospace-agent",
        description="Drive the karospace CLI with Claude to build a viewer "
        "(local hands, Claude brain, sanitized metadata only).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="Build a viewer from a raw .h5ad / .zarr.")
    b.add_argument("input", help="Path to the .h5ad file or SpatialData .zarr store.")
    b.add_argument(
        "intent",
        nargs="?",
        default="",
        help='Plain-English intent, e.g. "grid by sample, colour by cell_type, '
        'focus on Cd4/Cd8a/Gfap".',
    )
    b.add_argument(
        "--model",
        default=agent.DEFAULT_MODEL,
        help=f"Model alias or id (default: {agent.DEFAULT_MODEL}).",
    )

    c = sub.add_parser(
        "chat",
        help="Open a multi-turn conversation; optionally start with a build.",
    )
    c.add_argument(
        "input",
        nargs="?",
        default=None,
        help="Optional .h5ad / .zarr to build first; else just start talking.",
    )
    c.add_argument(
        "intent",
        nargs="?",
        default="",
        help="Plain-English intent for the opening build (with `input`).",
    )
    c.add_argument(
        "--model",
        default=agent.DEFAULT_MODEL,
        help=f"Model alias or id (default: {agent.DEFAULT_MODEL}).",
    )

    sub.add_parser(
        "auth",
        help="Show which Claude credential will be used, and how to sign in.",
    )

    w = sub.add_parser(
        "web",
        help="Serve a local browser chat over the same session (needs the "
        "'web' extra: pip install 'karospace-agent[web]').",
    )
    w.add_argument("input", nargs="?", default=None, help="Optional file to build first.")
    w.add_argument("intent", nargs="?", default="", help="Intent for the opening build.")
    w.add_argument("--host", default="127.0.0.1", help="Bind address (default: localhost only).")
    w.add_argument("--port", type=int, default=8765, help="Port (default: 8765).")
    w.add_argument(
        "--model",
        default=agent.DEFAULT_MODEL,
        help=f"Model alias or id (default: {agent.DEFAULT_MODEL}).",
    )

    a = sub.add_parser(
        "app",
        help="Open the chat in a native desktop window (the packaged-app face; "
        "needs the 'app' extra: pip install 'karospace-agent[app]').",
    )
    a.add_argument("input", nargs="?", default=None, help="Optional file to build first.")
    a.add_argument("intent", nargs="?", default="", help="Intent for the opening build.")
    a.add_argument(
        "--model",
        default=agent.DEFAULT_MODEL,
        help=f"Model alias or id (default: {agent.DEFAULT_MODEL}).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # A Finder-launched `.app` starts with a bare PATH, so tool lookups (and the
    # SDK's own `claude` lookup) would fail even when everything is installed.
    # Rehydrate from the login shell before any preflight. No-op in a terminal.
    if args.command == "app":
        from . import pathfix

        pathfix.hydrate_path()

    if args.command == "auth":
        text, code = auth_report()
        print(text)
        return code

    for note in _preflight():
        print(f"warning: {note}", file=sys.stderr)

    if args.command == "build":
        prompt = _compose_prompt(args.input, args.intent)
        try:
            asyncio.run(agent.run(prompt, model=args.model))
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130
        return 0

    if args.command == "chat":
        print(CHAT_BANNER, file=sys.stderr)
        print(auth.detect().line(), file=sys.stderr)
        try:
            asyncio.run(_chat(args.input, args.intent, args.model))
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nInterrupted.", file=sys.stderr)
            return 130
        return 0

    if args.command == "web":
        try:
            from . import web
        except ImportError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        first = _compose_prompt(args.input, args.intent) if args.input else None
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print(
                f"warning: binding {args.host} exposes the chat (no auth) to the "
                "network; anything typed there reaches the model.",
                file=sys.stderr,
            )
        print(f"karospace-agent web: http://{args.host}:{args.port}/  (Ctrl-C to stop)",
              file=sys.stderr)
        try:
            web.serve(host=args.host, port=args.port, model=args.model, opening_message=first)
        except KeyboardInterrupt:
            return 130
        return 0

    if args.command == "app":
        try:
            from . import desktop
        except ImportError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        first = _compose_prompt(args.input, args.intent) if args.input else None
        try:
            desktop.run_app(model=args.model, opening_message=first)
        except (ImportError, RuntimeError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
