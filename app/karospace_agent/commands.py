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
import threading
from collections.abc import Callable
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


# --- Progress sink ---------------------------------------------------------
#
# A ProgressSink receives each line a child process prints, as it prints it:
# `sink(stream, line)` with stream "stdout" | "stderr". It exists so a front end
# (the REPL, a web UI) can show live progress from a long export. It is a LOCAL
# side channel only: whatever the sink does, the model still receives nothing
# but the captured, sanitized/truncated string the tool layer returns. So
# karospace's own progress lines can flow to a console or browser freely with
# no data-boundary concern.

ProgressSink = Callable[[str, str], None]


def _stdout_isatty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _collapse_cr(text: str) -> str:
    """Collapse carriage-return progress redraws to their final frame per line.

    tqdm animates by rewriting one line with `\\r`. Live on the console that's a
    moving bar; but in the CAPTURED copy handed to the model it would be hundreds
    of redraw frames. For each `\\n`-delimited line, keep only what follows the
    last `\\r` — i.e. the bar's final state — so the model sees one clean line.
    """
    out = []
    for line in text.split("\n"):
        if "\r" in line:
            # Drop the CR the pty appends before every LF (ONLCR turns \n into
            # \r\n), then keep only the final redraw frame after the last \r.
            line = line.rstrip("\r").rsplit("\r", 1)[-1]
        out.append(line)
    return "\n".join(out)


def run(
    argv: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    on_line: ProgressSink | None = None,
    stream: bool = True,
) -> RunResult:
    """Run a command, capturing output while streaming progress locally, and never
    raise on non-zero exit.

    Two local delivery paths — both LOCAL side channels, so the model still gets
    only the captured, sanitized/truncated string the tool layer returns:

    * Interactive console + default sink: run the child under a pseudo-terminal so
      its tqdm bars (karospace's "Feature sidecar") think they're interactive and
      ANIMATE, instead of collapsing to a wall of plain log lines. Captured bytes
      are cr-collapsed to one clean line for the model.
    * A custom sink installed by a front end (REPL, web UI), or non-tty / stream
      off: pump each captured line to `sink(stream, line)` on its pump thread.

    `on_line` overrides the process-wide sink for this call (invoked from the pump
    threads, not the caller's). `stream=False` silences this run entirely — used
    for `--inspect-input`, whose raw stdout carries the example VALUES the boundary
    strips; showing them on the console/scrollback would surface locally what the
    tool then removes before the model sees it.
    """
    sink = null_sink if not stream else (on_line or get_progress_sink())
    use_pty = (
        stream
        and on_line is None
        and sink is console_sink
        and os.name == "posix"
        and _stdout_isatty()
    )
    if use_pty:
        try:
            return _run_pty(argv, timeout)
        except Exception:
            pass  # any pty trouble → fall back to the portable pipe path
    return _run_piped(argv, timeout, sink)


def _run_pty(argv: list[str], timeout: int) -> RunResult:
    """Run under a pty so child tqdm bars animate on the console; capture too."""
    import errno
    import fcntl
    import pty
    import select
    import struct
    import termios
    import time as _time

    master_fd, slave_fd = pty.openpty()
    # Give the pty a sane width/height so tqdm's dynamic_ncols bar renders full.
    try:
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 120, 0, 0))
    except Exception:
        pass
    try:
        proc = subprocess.Popen(
            argv,
            stdout=slave_fd,
            stderr=slave_fd,
            stdin=slave_fd,
            cwd=str(REPO_ROOT),
            close_fds=True,
        )
    except FileNotFoundError as e:
        os.close(master_fd)
        os.close(slave_fd)
        return RunResult(127, "", f"Executable not found: {e}")
    os.close(slave_fd)  # parent keeps only the master end

    chunks: list[bytes] = []
    deadline = _time.monotonic() + timeout
    timed_out = False
    try:
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                timed_out = True
                proc.kill()
                break
            try:
                readable, _, _ = select.select([master_fd], [], [], min(remaining, 1.0))
            except (OSError, ValueError):
                break
            if not readable:
                if proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(master_fd, 65536)
            except OSError as e:
                if e.errno == errno.EIO:  # Linux signals child exit via EIO
                    break
                raise
            if not data:  # EOF (macOS)
                break
            chunks.append(data)
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except Exception:
                pass  # a closed/broken console must never break the run
    finally:
        os.close(master_fd)

    proc.wait()
    captured = _collapse_cr(b"".join(chunks).decode("utf-8", errors="replace"))
    if timed_out:
        return RunResult(124, captured, f"\nTimed out after {timeout}s.", timed_out=True)
    return RunResult(proc.returncode, captured, "")


def console_sink(stream: str, line: str) -> None:
    """Default sink: tee to this process's own stdout/stderr."""
    target = sys.stdout if stream == "stdout" else sys.stderr
    try:
        target.write(line)
        target.flush()
    except Exception:
        pass  # a closed/broken console must never break the run


def null_sink(stream: str, line: str) -> None:
    """Silent sink (KAROSPACE_AGENT_STREAM=0, or a host that captures output
    some other way)."""


def _default_sink() -> ProgressSink:
    return null_sink if os.environ.get("KAROSPACE_AGENT_STREAM", "1") == "0" else console_sink


_progress_sink: ProgressSink | None = None


def set_progress_sink(sink: ProgressSink | None) -> None:
    """Install a process-wide sink for child-process progress lines.

    `None` restores the default (console, or silent under
    KAROSPACE_AGENT_STREAM=0). Process-wide because the tool handlers that
    spawn subprocesses have no per-call hook; one front end per process is the
    intended shape (a REPL or a web server owning one Session)."""
    global _progress_sink
    _progress_sink = sink


def get_progress_sink() -> ProgressSink:
    return _progress_sink if _progress_sink is not None else _default_sink()


def _run_piped(argv: list[str], timeout: int, sink: ProgressSink) -> RunResult:
    """Portable path: capture stdout/stderr separately, streaming each line to
    `sink(stream, line)` on its pump thread."""
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered, so progress shows as it arrives
            cwd=str(REPO_ROOT),
        )
    except FileNotFoundError as e:
        return RunResult(127, "", f"Executable not found: {e}")

    out_chunks: list[str] = []
    err_chunks: list[str] = []

    def pump(src, stream: str, acc: list[str]) -> None:
        for line in iter(src.readline, ""):
            acc.append(line)
            try:
                sink(stream, line)
            except Exception:
                pass  # a misbehaving sink must never break the run
        src.close()

    t_out = threading.Thread(target=pump, args=(proc.stdout, "stdout", out_chunks), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, "stderr", err_chunks), daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        t_out.join()
        t_err.join()
        return RunResult(
            124,
            "".join(out_chunks),
            "".join(err_chunks) + f"\nTimed out after {timeout}s.",
            timed_out=True,
        )

    t_out.join()
    t_err.join()
    return RunResult(proc.returncode, "".join(out_chunks), "".join(err_chunks))


def run_karospace(
    args: list[str], timeout: int = DEFAULT_TIMEOUT, stream: bool = True
) -> RunResult:
    binp = karospace_bin()
    if binp is None:
        return RunResult(127, "", "karospace not found on PATH (set KAROSPACE_BIN).")
    return run([binp, *args], timeout=timeout, stream=stream)


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
