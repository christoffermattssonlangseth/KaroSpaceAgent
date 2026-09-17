"""`karospace-agent build <input> "<intent>"` — the entry point.

Composes the user's file + plain-English intent into the first message and hands
it to the agent loop. The model does the rest through the sanitizing tools.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import agent
from .commands import companion_bin, karospace_bin


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

    return 1


if __name__ == "__main__":
    sys.exit(main())
