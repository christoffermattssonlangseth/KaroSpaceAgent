"""The model's entire capability surface: sanitizing wrappers over the CLIs.

Each `@tool` runs a local subprocess (via `commands.py`) and returns only
sanitized, truncated text (via `sanitize.py`). There is deliberately no tool
that reads file bytes — the model can inspect metadata, run the pipeline, and
stat the output, but it can never pull the expression matrix or coordinates
into context. Built-in Read/Bash tools are disabled in `agent.py`, so this list
is the whole of what Claude can do.
"""

from __future__ import annotations

from typing import Annotated, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import commands
from .sanitize import stat_paths, strip_inspect_examples, truncate

SERVER_NAME = "karospace"


def _result(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


def _report(rr: commands.RunResult, label: str) -> dict[str, Any]:
    """Render a RunResult for the model: exit code + truncated stdout/stderr."""
    body = (
        f"$ {label}\n"
        f"exit code: {rr.returncode}"
        + ("  (TIMED OUT)" if rr.timed_out else "")
        + "\n\n--- stdout ---\n"
        + truncate(rr.stdout)
        + "\n\n--- stderr ---\n"
        + truncate(rr.stderr)
    )
    return _result(body, is_error=not rr.ok)


# --- Inspection -----------------------------------------------------------

@tool(
    "inspect_input",
    "Inspect a raw .h5ad / SpatialData .zarr and return the SCHEMA only: obs "
    "column names, type, cardinality (number of distinct values), missing counts, "
    "and feature counts by modality. Runs `karospace <input> --inspect-input` "
    "without building a viewer. Always call this first. Example VALUES are "
    "deliberately withheld (compliance: cell coordinates, sample IDs, and other "
    "values must not leave the local machine) — reason from column names, types, "
    "and cardinalities. The expression matrix and coordinates are never exposed.",
    {
        "input_path": Annotated[str, "Path to the .h5ad file or .zarr store."],
        "spatialdata_table": Annotated[
            str, "For SpatialData .zarr with multiple tables, the table key. Else ''."
        ],
    },
)
async def inspect_input(args: dict[str, Any]) -> dict[str, Any]:
    argv = [args["input_path"], "--inspect-input"]
    table = (args.get("spatialdata_table") or "").strip()
    if table:
        argv += ["--spatialdata-table", table]
    # stream=False: do NOT live-tee this run. Its raw stdout carries the example
    # VALUES the boundary strips — teeing would print them to the local console
    # and scrollback before the strip below removes them from the model's view.
    rr = commands.run_karospace(argv, timeout=600, stream=False)
    # Enforce the boundary: strip example VALUES, keep only the schema.
    rr.stdout = strip_inspect_examples(rr.stdout)
    return _report(rr, f"karospace {' '.join(argv)}")


@tool(
    "cli_help",
    "Show `karospace --help` (or a verb's help) so you can verify a flag exists "
    "before using it. Never invent flags — check here.",
    {"verb": Annotated[str, "One of '', 'package-sidecar', 'ome-convert'."]},
)
async def cli_help(args: dict[str, Any]) -> dict[str, Any]:
    verb = (args.get("verb") or "").strip()
    argv = ([verb] if verb else []) + ["--help"]
    rr = commands.run_karospace(argv, timeout=60)
    # argparse prints help to stdout and exits 0; some verbs exit 0 too.
    return _result(truncate(rr.stdout or rr.stderr))


# --- Pre-processing -------------------------------------------------------

@tool(
    "merge_sections",
    "Merge per-section .h5ad files into one KaroSpace-ready file, adding "
    "sample_id / condition / sample_batch. Use when the input is a single "
    "section stripped of sample metadata (the single-section trap) and the user "
    "supplies the sibling files. Returns the merge log (cell counts, obsm/layers "
    "keys) — no cell-level data.",
    {
        "sections": Annotated[
            list, "Each item 'sample_id:path[:condition]', one per section file."
        ],
        "output": Annotated[str, "Output merged .h5ad path."],
    },
)
async def merge_sections(args: dict[str, Any]) -> dict[str, Any]:
    sections = [str(s) for s in args["sections"]]
    rr = commands.run_merge(sections, args["output"])
    return _report(rr, f"merge_sections.py -> {args['output']}")


@tool(
    "run_companion",
    "Run the karospace-companion pre-processor (Rust) with the given arguments, "
    "e.g. ['prepare', '<in.h5ad>', '--delaunay', '--groupby', 'sample_id', "
    "'--output', '<enriched.h5ad>']. Use to add a spatial neighbor graph or "
    "baked-in analytics before export. Returns the companion log only.",
    {"args": Annotated[list, "Argument tokens passed straight to the binary."]},
)
async def run_companion(args: dict[str, Any]) -> dict[str, Any]:
    tokens = [str(a) for a in args["args"]]
    rr = commands.run_companion(tokens)
    return _report(rr, f"karospace-companion {' '.join(tokens)}")


# --- Export & package -----------------------------------------------------

@tool(
    "run_export",
    "Run the karospace export. Provide the input, the output path, and the full "
    "list of flag tokens you chose from the metadata (e.g. ['--section-key', "
    "'sample_id', '--main-cell-annotation', 'cell_type', '--feature-storage', "
    "'sidecar']). Returns exit code + stdout/stderr so you can read errors and "
    "iterate. A bad --section-key surfaces as a traceback ending in ValueError; "
    "read it and fix the flag. Does NOT return viewer contents.",
    {
        "input_path": Annotated[str, "Input .h5ad / .zarr path."],
        "output": Annotated[str, "Output viewer .html path."],
        "flags": Annotated[
            list, "CLI flag tokens (each flag and its value as separate items)."
        ],
    },
)
async def run_export(args: dict[str, Any]) -> dict[str, Any]:
    flags = [str(f) for f in args.get("flags", [])]
    argv = [args["input_path"], "-o", args["output"], *flags]
    rr = commands.run_karospace(argv)
    return _report(rr, f"karospace {' '.join(argv)}")


@tool(
    "package_sidecar",
    "Package an existing sidecar viewer.html into a single-file .karospace plus "
    "its .loader.html (no recompute). Run after a successful sidecar export to "
    "produce the second deliverable. Runs `karospace package-sidecar <html> "
    "[-o <out.karospace>]`.",
    {
        "html_path": Annotated[str, "Path to the sidecar viewer.html."],
        "output": Annotated[str, "Output .karospace path, or '' for the default."],
    },
)
async def package_sidecar(args: dict[str, Any]) -> dict[str, Any]:
    argv = ["package-sidecar", args["html_path"]]
    out = (args.get("output") or "").strip()
    if out:
        argv += ["-o", out]
    rr = commands.run_karospace(argv, timeout=600)
    return _report(rr, f"karospace {' '.join(argv)}")


@tool(
    "validate_output",
    "Report existence, kind, and size of output artifacts so you can confirm the "
    "viewer actually wrote. Pass every path you expect (viewer.html, and for a "
    "sidecar also viewer.features.json + viewer.features/, and for a package the "
    ".karospace + .loader.html). Returns file metadata only, never contents.",
    {"paths": Annotated[list, "Paths to stat."]},
)
async def validate_output(args: dict[str, Any]) -> dict[str, Any]:
    paths = [str(p) for p in args.get("paths", [])]
    if not paths:
        return _result("No paths given.", is_error=True)
    return _result(stat_paths(paths))


ALL_TOOLS = [
    inspect_input,
    cli_help,
    merge_sections,
    run_companion,
    run_export,
    package_sidecar,
    validate_output,
]

TOOL_NAMES = [t.name for t in ALL_TOOLS]

# Fully-qualified names as the SDK addresses in-process MCP tools:
# mcp__<server>__<tool>. Used to auto-allow exactly these and nothing else.
ALLOWED_TOOL_NAMES = [f"mcp__{SERVER_NAME}__{name}" for name in TOOL_NAMES]


def build_server():
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=ALL_TOOLS)
