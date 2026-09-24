"""Private, local tool-run records. Never include these records in model context.

No dataset contents, transcripts, credentials, or raw logs are recorded. Paths,
decoded tool arguments and command lines can still identify people; files are
owner-only and served only through the local history view, never through MCP.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid


_command_sink = ContextVar("local_history_command_sink", default=None)


def now():
    return datetime.now(timezone.utc).isoformat()


def history_root():
    override = os.environ.get("KAROSPACE_AGENT_HISTORY_DIR")
    return Path(override).expanduser() if override else Path.home() / ".karospace-agent" / "history"


@lru_cache(maxsize=1)
def environment_versions():
    packages = {}
    for name in ("karospace-agent", "claude-agent-sdk", "mcp", "karospace",
                 "rdstoh5ad", "anndata", "numpy", "scipy", "scanpy", "h5py"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    return {"python": platform.python_version(), "interpreter": sys.executable,
            "packages": packages, "scope": "app interpreter"}


@lru_cache(maxsize=32)
def executable_version(executable, modified_ns=None):
    """Best-effort local version probe of the fixed workflow executables."""
    name = Path(executable).name.lower()
    if not (name.startswith("python") or name in {"karospace", "karospace-companion", "rds2h5ad"}):
        return None
    try:
        process = subprocess.run([executable, "--version"], capture_output=True,
                                 text=True, timeout=3)
        if process.returncode == 0:
            lines = (process.stdout or process.stderr).strip().splitlines()
            return lines[0][:256] if lines else None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def file_metadata(value):
    path = Path(value).expanduser().absolute()
    entry = {"path": str(path)}
    try:
        stat = path.stat()
        entry.update(exists=True, kind="directory" if path.is_dir() else "file",
                     size_bytes=stat.st_size, modified_ns=stat.st_mtime_ns)
    except FileNotFoundError:
        entry["exists"] = False
    except OSError:
        entry["metadata_unavailable"] = True
    return entry


@contextmanager
def capture_commands(sink):
    token = _command_sink.set(sink)
    try:
        yield
    finally:
        _command_sink.reset(token)


def accept_command(command):
    """A worker's command metadata travels only over its private parent pipe."""
    sink = _command_sink.get()
    if sink is not None:
        sink(command)


def record_command(argv, timeout, cwd):
    if _command_sink.get() is None:
        return
    command = {"argv": list(argv), "timeout_seconds": timeout, "cwd": str(cwd)}
    executable = shutil.which(argv[0]) or argv[0]
    command["executable"] = file_metadata(executable)
    command["executable"]["version"] = executable_version(
        executable, command["executable"].get("modified_ns"))
    # Hash local script source, never input datasets or generated artifacts.
    scripts = []
    for value in argv[1:2]:
        path = Path(value)
        if path.suffix == ".py" and path.is_file() and path.parent == Path(cwd) / "scripts":
            scripts.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    command["scripts"] = scripts
    accept_command(command)


class RunHistory:
    def __init__(self, provider="mcp", model=None, root=None):
        self.root = Path(root) if root is not None else history_root()
        self.session_id = uuid.uuid4().hex
        self.provider, self.model = provider, model
        self.error = None

    def _write(self, record):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise OSError("History directory must not be a symlink.")
        self.root.chmod(0o700)
        folder = self.root / self.session_id
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.is_symlink():
            raise OSError("History session must not be a symlink.")
        folder.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix=".record-", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump(record, target, ensure_ascii=False, allow_nan=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, folder / (record["id"] + ".json"))
        finally:
            Path(temporary).unlink(missing_ok=True)

    def begin(self, tool, arguments):
        # Serialize now to detach parameters from mutable handler dictionaries.
        arguments = json.loads(json.dumps(arguments, allow_nan=False))
        from .recovery import RECOVERABLE, io_paths, fingerprint, RecoveryError
        inputs, _ = io_paths(tool, arguments)
        record = {"version": 1, "id": uuid.uuid4().hex, "session_id": self.session_id,
                  "provider": self.provider, "model": self.model, "tool": tool,
                  "started_at": now(), "status": "started", "arguments": arguments,
                  "inputs": [file_metadata(p) for p in inputs], "outputs": [],
                  "environment": environment_versions(), "commands": []}
        record["owner_pid"] = os.getpid()
        if tool in RECOVERABLE:
            record["input_fingerprints"] = []
            for path in inputs:
                try:
                    record["input_fingerprints"].append(fingerprint(path))
                except (OSError, RecoveryError):
                    record["input_fingerprints"].append({"path": str(path), "verified": False})
        self._write(record)
        return record

    def command(self, record, command):
        record["commands"].append(command)
        self._write(record)

    def finish(self, record, status, response=None):
        record.update(status=status, finished_at=now())
        if response:
            # Save only the already-filtered outcome, never the raw local report.
            record["outcome"] = json.loads(response["content"][0]["text"])
        from .recovery import RECOVERABLE, io_paths, fingerprint, RecoveryError
        _, outputs = io_paths(record["tool"], record["arguments"])
        record["outputs"] = [file_metadata(path) for path in outputs]
        if record["tool"] in RECOVERABLE and status == "completed":
            record["output_fingerprints"] = []
            for path in outputs:
                # Dataset checkpoints only. A viewer's sidecars cannot be
                # validated from a checksum of its HTML alone.
                if Path(path).suffix.lower() not in {".h5ad", ".zarr"}:
                    continue
                try:
                    record["output_fingerprints"].append(fingerprint(path))
                except (OSError, RecoveryError):
                    record["output_fingerprints"].append({"path": str(path), "verified": False})
        try:
            self._write(record)
        except (OSError, ValueError, TypeError):
            # Work may already be complete. Keep its result, but make failure
            # visible in the local history view; the durable start remains.
            self.error = "History could not save the final status of a tool run."

    def get(self, session_id, run_id):
        if any(len(value) != 32 or any(c not in "0123456789abcdef" for c in value)
               for value in (session_id, run_id)):
            raise ValueError("invalid_record_id")
        folder = self.root / session_id
        path = folder / (run_id + ".json")
        if self.root.is_symlink() or folder.is_symlink() or path.is_symlink():
            raise ValueError("invalid_record_path")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("session_id") != session_id or record.get("id") != run_id:
            raise ValueError("invalid_record_identity")
        return record

    def recent(self, limit=100):
        records = []
        if not self.root.exists():
            return records
        if self.root.is_symlink():
            raise OSError("History directory must not be a symlink.")
        for session in self.root.iterdir():
            if session.is_symlink() or not session.is_dir() or len(session.name) != 32:
                continue
            for path in session.glob("*.json"):
                if path.is_symlink():
                    continue
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(record, dict) and record.get("version") == 1:
                        records.append(record)
                except (OSError, ValueError):
                    continue
        records.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return records[:limit]
