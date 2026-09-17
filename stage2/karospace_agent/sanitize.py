"""The data-handling boundary, in code.

Everything a tool is about to hand back to the model passes through here first.
The rule (data can be sensitive human data under GDPR governance, so
the boundary is always-on regardless of the dataset): per the data management
plan only the SCHEMA crosses to the model — column names, dtypes,
cardinalities, missing/aggregate counts, CLI help, error text. Never any data
VALUES: not the expression matrix, coordinates, patient identifiers, sample IDs,
or the inspect report's example values.

Three guarantees this module provides:

1. `strip_inspect_examples` drops the ` examples: ...` tail from an inspect
   report, so per-column example values (which include literal coordinates and
   category labels) never reach the model — only the structural schema does.

2. `truncate` caps the size of any subprocess output. The karospace/companion
   CLIs already emit metadata + logs by construction (never the matrix), but a
   runaway log or an accidental data dump should never flood the model context.

3. `stat_paths` is the *only* way a tool reports on a file, and it reports
   metadata about the file (exists / size / kind), never its contents. No tool
   in this app reads the bytes of an .h5ad or a viewer.html into the model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# How much subprocess text we are ever willing to show the model. Errors and
# inspect reports are far smaller than this; the cap only bites on pathological
# output. We keep the head and the tail because a traceback's useful line
# (the final `ValueError: ...`) lives at the very end.
MAX_OUTPUT_CHARS = 20_000


def strip_inspect_examples(text: str) -> str:
    """Drop per-column example VALUES from a `karospace --inspect-input` report.

    Compliance (data management plan): only the *schema* may cross to the LLM —
    column names, type, cardinality, missing/aggregate counts — never the values
    themselves. The inspect report's ` examples: ...` tail carries literal cell
    coordinates and category labels (e.g. sample IDs), so it is removed here.
    Everything structural on the line is kept:

        `  - x_centroid [numeric; 38,270 values] examples: "1101.8", ...`
     -> `  - x_centroid [numeric; 38,270 values]`
    """
    out = []
    for line in text.splitlines():
        idx = line.find(" examples:")
        out.append(line[:idx] if idx != -1 else line)
    return "\n".join(out)


def truncate(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """Clip text to `max_chars`, preserving head and tail with a marker."""
    if text is None:
        return ""
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    head, tail = text[:keep], text[-keep:]
    dropped = len(text) - 2 * keep
    return f"{head}\n\n...[{dropped} characters truncated]...\n\n{tail}"


@dataclass
class PathStat:
    path: str
    exists: bool
    kind: str  # "file" | "dir" | "missing"
    size_bytes: int | None

    def human(self) -> str:
        if not self.exists:
            return f"MISSING  {self.path}"
        size = "" if self.size_bytes is None else f"  ({_human_size(self.size_bytes)})"
        return f"{self.kind:7} {self.path}{size}"


def stat_path(path: str) -> PathStat:
    """Metadata about a path — never its contents."""
    if not os.path.exists(path):
        return PathStat(path=path, exists=False, kind="missing", size_bytes=None)
    if os.path.isdir(path):
        return PathStat(path=path, exists=True, kind="dir", size_bytes=_dir_size(path))
    return PathStat(
        path=path, exists=True, kind="file", size_bytes=os.path.getsize(path)
    )


def stat_paths(paths: list[str]) -> str:
    """A human-readable table of path metadata for the model to validate output."""
    return "\n".join(stat_path(p).human() for p in paths)


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def _human_size(n: int) -> str:
    step = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if step < 1024 or unit == "TB":
            return f"{step:.1f} {unit}" if unit != "B" else f"{int(step)} B"
        step /= 1024
    return f"{n} B"
