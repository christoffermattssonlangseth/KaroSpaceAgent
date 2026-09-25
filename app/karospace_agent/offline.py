"""Launch the macOS offline app with an inherited network/filesystem policy."""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import uuid

from . import commands, isolation


UI_SERVICES = ("com.apple.windowserver.active",)
ROOT = Path(__file__).resolve().parents[2]


def model_path(value=None):
    if value:
        path = Path(value).expanduser().resolve(strict=True)
    else:
        cache = Path.home() / ".cache/huggingface/hub"
        candidates = [ROOT / "output/offline-models/qwen2.5-0.5b-instruct-4bit"]
        for name in ("Qwen2.5-0.5B-Instruct-4bit", "Qwen3-4B-Instruct-2507-4bit"):
            candidates.extend(sorted((cache / ("models--mlx-community--" + name) / "snapshots").glob("*")))
        path = next((p for p in candidates if (p / "config.json").is_file()), None)
        if path is None:
            raise ValueError("Choose an already-downloaded MLX model with --local-model. Offline mode never downloads models.")
    if not path.is_dir() or not (path / "config.json").is_file() or not list(path.glob("*.safetensors")):
        raise ValueError("The local model needs config.json and complete .safetensors weights.")
    return path


def runtime_path(value=None):
    local = ROOT / ".venv-offline/bin/python"
    path = Path(value).expanduser() if value else (local if local.is_file() else Path(sys.executable))
    return path.absolute()  # Keep a venv's executable path, not only its symlink target.


def profile(read_roots, write_root, read_directories=(), probe_root=None):
    def quoted(path):
        value = str(Path(path).resolve())
        if "\0" in value:
            raise ValueError("invalid_sandbox_path")
        return json.dumps(value)
    rules = [isolation.PROFILE, "(deny file-read-data)", "(deny file-write*)"]
    # dyld opens the root directory during startup. This grants the directory
    # itself, not its contents recursively; outside data files remain denied.
    rules.append('(allow file-read-data (literal "/"))')
    rules.extend(f"(allow file-read-data (literal {quoted(p)}))" for p in read_directories)
    rules.extend(f"(allow file-read-data (subpath {quoted(p)}))" for p in sorted(set(map(str, read_roots))))
    rules.append(f"(allow file-read-data file-write* (subpath {quoted(write_root)}))")
    if probe_root is not None:
        # Only synthetic probe markers/socket live here. A short absolute path
        # is required by macOS sockaddr_un, regardless of the user's home path.
        rules.append(f"(allow file-read-data file-write* (subpath {quoted(probe_root)}))")
    # Native Tk only; no browser/WebKit, network services or launch services.
    rules.append("(allow mach-lookup " + " ".join(f"(global-name {json.dumps(s)})" for s in UI_SERVICES) + ")")
    rules.append('(allow file-read-data (literal "/dev/urandom") (literal "/dev/random"))')
    rules.append('(allow file-read-data file-write* (literal "/dev/null"))')
    return "\n".join(rules)


def clean_environment(workspace, runtime):
    env = {
        "PATH": os.pathsep.join((str(runtime.parent), str(Path(sys.base_prefix) / "bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin")),
        "HOME": str(workspace / "home"), "TMPDIR": str(workspace / "tmp"),
        "XDG_CACHE_HOME": str(workspace / "cache"), "MPLCONFIGDIR": str(workspace / "cache/matplotlib"),
        "NUMBA_CACHE_DIR": str(workspace / "cache/numba"),
        "HF_HOME": str(workspace / "cache/huggingface"), "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1", "TOKENIZERS_PARALLELISM": "false", "PYTHONDONTWRITEBYTECODE": "1",
        "KAROSPACE_AGENT_HISTORY_DIR": str(workspace / "history"),
        "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
        "KAROSPACE_MERGE_PYTHON": str(runtime),
    }
    # Fixed local executables only. Do not inherit provider credentials, proxies,
    # model endpoints, arbitrary Python paths, or dynamic-library overrides.
    for variable, finder in (("KAROSPACE_BIN", commands.karospace_bin),
                             ("KAROSPACE_COMPANION", commands.companion_bin),
                             ("RDS2H5AD_BIN", commands.rds2h5ad_bin)):
        found = finder()
        if found:
            env[variable] = str(Path(found).resolve())
    return env


def launch(*, surface="app", model=None, runtime=None, input_path=None, intent="",
           workspace_parent=None, smoke_test=False):
    """Run one confined session; no branch retries without the sandbox."""
    isolation.isolated_command(["true"])  # Refuse unsupported OS before creating state.
    selected_model, python = model_path(model), runtime_path(runtime)
    if not python.is_file():
        raise ValueError("The offline Python runtime is missing. Install the offline extra first.")
    selected_input = Path(input_path).expanduser().resolve(strict=True) if input_path else None
    parent = Path(workspace_parent).expanduser() if workspace_parent else Path.home() / "Library/Application Support/KaroSpaceAgent/offline"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = parent.resolve() / uuid.uuid4().hex
    workspace.mkdir(mode=0o700)
    for folder in ("home", "tmp", "cache", "output", "history"):
        (workspace / folder).mkdir(mode=0o700)
    env = clean_environment(workspace, python)
    # Read-only executable/library roots; the selected input is a separate grant.
    roots = {ROOT / "scripts", Path(__file__).parent, Path(sys.base_prefix), Path(sys.prefix), python.parent.parent,
             python.resolve().parent.parent, Path("/System"), Path("/usr/lib"),
             Path("/usr/share"), Path("/usr/bin"), Path("/bin"), Path("/usr/sbin"),
             Path("/sbin"), Path("/Library/Frameworks"), selected_model}
    directories = {ROOT, ROOT / "app"}
    # Editable scientific packages may live outside the interpreter prefix.
    # Grant their package code, not sibling datasets or repository secrets.
    for package in ("karospace", "rdstoh5ad"):
        spec = importlib.util.find_spec(package)
        if spec and spec.submodule_search_locations:
            for location in spec.submodule_search_locations:
                root = Path(location).resolve()
                roots.add(root)
                directories.add(root.parent)
    # HF snapshots use links into a content-addressed cache. Grant only their
    # resolved files, not the whole cache or the user's home directory.
    roots.update(p.resolve() for p in selected_model.rglob("*") if p.is_file())
    for variable in ("KAROSPACE_BIN", "KAROSPACE_COMPANION", "RDS2H5AD_BIN"):
        if variable in env:
            roots.add(Path(env[variable]))
    if selected_input:
        roots.add(selected_input)
    config = {"surface": surface, "model": str(selected_model), "workspace": str(workspace),
              "input_path": str(selected_input) if selected_input else None, "intent": intent,
              "smoke_test": smoke_test}
    print(f"Offline session files: {workspace}", flush=True)
    # The live outside listener and file are evidence for the actual process,
    # not an environment flag or a successful check in a different process.
    with tempfile.TemporaryDirectory(prefix="karo-outside-", dir="/private/tmp") as outside, \
            tempfile.TemporaryDirectory(prefix="karo-probe-", dir="/private/tmp") as probe:
        outside = Path(outside)
        (outside / "read-test").write_text("synthetic isolation test", encoding="utf-8")
        config["outside_probe"] = str(outside)
        config["probe_directory"] = probe
        with isolation._unix_listener(probe):
            config_path = workspace / "session.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            config_path.chmod(0o600)
            argv = [str(isolation.SANDBOX_EXEC), "-p", profile(roots, workspace, directories, probe_root=probe),
                    str(python), "-I", "-B", str(Path(__file__).with_name("offline_bootstrap.py")), str(config_path)]
            proc = subprocess.Popen(argv, env=env, cwd=workspace, start_new_session=True)
            try:
                code = proc.wait()
                if code < 0:
                    print("Offline process stopped before completing. No unconfined or cloud fallback was started.", file=sys.stderr)
                    return 2
                return code
            except KeyboardInterrupt:
                return 130
            finally:
                # Include scientific children when the window closes or the
                # launcher is interrupted. All inherited the sandbox too.
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=3)
                    finally:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                except ProcessLookupError:
                    pass
