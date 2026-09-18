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
import sys

from . import agent
from .commands import companion_bin, karospace_bin

QUIT_COMMANDS = {"/quit", "/exit", "/q"}
PROMPT = "\nyou> "

CHAT_BANNER = """\
karospace-agent chat — type a message, /quit to leave (Ctrl-D also works).
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
    """Blocking stdin read; None on EOF. Runs in a worker thread from the loop."""
    try:
        return input(prompt)
    except EOFError:
        return None


async def chat_loop(
    session: agent.Session,
    first_message: str | None = None,
    read_line=_read_line,
) -> None:
    """The REPL: optional opening build request, then user turns until quit/EOF.

    `read_line` is injectable so tests can script a conversation."""
    if first_message:
        await session.send(first_message)
    while True:
        line = await asyncio.to_thread(read_line)
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        if line.lower() in QUIT_COMMANDS:
            break
        await session.send(line)


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
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
