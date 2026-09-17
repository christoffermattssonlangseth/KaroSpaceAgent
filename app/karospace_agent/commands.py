"""The local hands: subprocess wrappers around the real binaries.

Nothing here talks to the model. These functions locate the three executables
this workflow drives and run them with a fixed leading token, so the model can
choose *arguments* but never *which program runs*:

    karospace                (Python, on PATH)          -> export / inspect / package
    karospace-companion      (Rust, sibling repo)       -> pre-processing
    scripts/merge_sections.py (this repo)               -> merge per-section files

All runs are `shell=False` with an argument list, so there is no shell to
inject into. Output is captured and returned verbatim; sanitizing/truncation is
the caller's job (see `sanitize.py`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# app/karospace_agent/commands.py -> repo root is parents[2] (karospace_agent, app, root).
REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE_SCRIPT = REPO_ROOT / "scripts" / "merge_sections.py"

# Default timeout for a full export (analytics + DE + pathway can be minutes on
# large data). Override with KAROSPACE_AGENT_TIMEOUT (seconds).
DEFAULT_TIMEOUT = int(os.environ.get("KAROSPACE_AGENT_TIMEOUT", "3600"))


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def karospace_bin() -> str | None:
    """`karospace` from PATH, or KAROSPACE_BIN if set."""
    return os.environ.get("KAROSPACE_BIN") or shutil.which("karospace")


def companion_bin() -> str | None:
    """The Rust companion binary. Env override, else the sibling-repo default."""
    override = os.environ.get("KAROSPACE_COMPANION")
    if override:
        return override if os.path.exists(override) else None
    default = (
        REPO_ROOT.parent
        / "KaroSpaceCompanion"
        / "target"
        / "release"
        / "karospace-companion"
    )
    return str(default) if default.exists() else None


def merge_python() -> str:
    """Interpreter used to run merge_sections.py.

    The merge script needs the scientific stack (anndata/pandas), which lives in
    the karospace environment — not necessarily this app's own venv. Prefer the
    python next to the `karospace` executable; fall back to our own interpreter.
    Override with KAROSPACE_MERGE_PYTHON.
    """
    override = os.environ.get("KAROSPACE_MERGE_PYTHON")
    if override:
        return override
    binp = karospace_bin()
    if binp:
        candidate = os.path.join(os.path.dirname(binp), "python")
        if os.path.exists(candidate):
            return candidate
    return sys.executable


def run(argv: list[str], timeout: int = DEFAULT_TIMEOUT) -> RunResult:
    """Run a command, capture stdout/stderr, never raise on non-zero exit."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
        )
        return RunResult(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return RunResult(124, out, err + f"\nTimed out after {timeout}s.", timed_out=True)
    except FileNotFoundError as e:
        return RunResult(127, "", f"Executable not found: {e}")


def run_karospace(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> RunResult:
    binp = karospace_bin()
    if binp is None:
        return RunResult(127, "", "karospace not found on PATH (set KAROSPACE_BIN).")
    return run([binp, *args], timeout=timeout)


def run_companion(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> RunResult:
    binp = companion_bin()
    if binp is None:
        return RunResult(
            127,
            "",
            "karospace-companion not found. Build ../KaroSpaceCompanion "
            "(cargo build --release) or set KAROSPACE_COMPANION.",
        )
    return run([binp, *args], timeout=timeout)


def run_merge(sections: list[str], output: str, timeout: int = DEFAULT_TIMEOUT) -> RunResult:
    """Run scripts/merge_sections.py. `sections` are `sample_id:path[:condition]`."""
    if not MERGE_SCRIPT.exists():
        return RunResult(127, "", f"merge script missing: {MERGE_SCRIPT}")
    argv = [merge_python(), str(MERGE_SCRIPT)]
    for spec in sections:
        argv += ["--section", spec]
    argv += ["--output", output]
    return run(argv, timeout=timeout)
