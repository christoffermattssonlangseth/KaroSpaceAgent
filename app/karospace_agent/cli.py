"""`karospace-agent build <input> "<intent>"` and `karospace-agent chat` — the
entry points.

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

from . import agent
from .commands import companion_bin, karospace_bin

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
    if karospace_bin() is None:
        notes.append("karospace not found on PATH (set KAROSPACE_BIN) — required.")
    if companion_bin() is None:
        notes.append(
            "karospace-companion not found — companion pre-processing unavailable "
            "(build ../KaroSpaceCompanion or set KAROSPACE_COMPANION)."
        )
    return notes


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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

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
        try:
            asyncio.run(_chat(args.input, args.intent, args.model))
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nInterrupted.", file=sys.stderr)
            return 130
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
