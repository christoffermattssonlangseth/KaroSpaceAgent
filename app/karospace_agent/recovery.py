"""Verify local checkpoints and prepare an aliased, human-reviewed continuation.

Never restore raw transcripts or send history records to either provider.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import uuid


OUTPUT_TOOLS = {"qc_filter", "run_preprocess", "split_sections", "generate_notebook",
                "rds_convert", "geo_build", "ingest_spatial", "merge_sections", "run_export"}
RECOVERABLE = OUTPUT_TOOLS | {"run_companion"}


class RecoveryError(ValueError):
    pass


def io_paths(name, arguments):
    """Explicit workflow paths only; never guess file paths from arbitrary text."""
    inputs = [arguments[k] for k in ("input_path", "html_path") if arguments.get(k)]
    inputs.extend(arguments.get("paths", []))
    outputs = [arguments[k] for k in ("output", "output_dir") if arguments.get(k)]
    if name == "merge_sections":
        for section in arguments.get("sections", []):
            fields = section.split(":")
            if len(fields) not in (2, 3):
                raise RecoveryError("unsupported_merge_specification")
            inputs.append(fields[1])
    if name == "run_companion":
        tokens = arguments.get("args", [])
        if len(tokens) < 2 or tokens[0] != "prepare":
            return [], []
        inputs = [tokens[1]]
        for index, token in enumerate(tokens):
            if token == "--output" and index + 1 < len(tokens):
                outputs.append(tokens[index + 1])
            elif token.startswith("--output="):
                outputs.append(token.split("=", 1)[1])
    return inputs, outputs


def fingerprint(value):
    """Stream file/directory bytes locally; hashes themselves stay local too."""
    path = Path(value).expanduser().absolute()
    if path.is_symlink() or not path.exists():
        raise RecoveryError("checkpoint_missing_or_linked")
    paths = [path] if path.is_file() else sorted(path.rglob("*"))
    digest, total, count = hashlib.sha256(), 0, 0
    initial = path.stat()
    before_stats = {member: member.stat() for member in paths}
    for member in paths:
        if member.is_symlink():
            raise RecoveryError("checkpoint_missing_or_linked")
        if member.is_dir():
            continue
        before = member.stat()
        if not member.is_file():
            raise RecoveryError("unsupported_checkpoint")
        label = member.relative_to(path).as_posix() if path.is_dir() else "file"
        digest.update(label.encode("utf-8") + b"\0" + str(before.st_size).encode() + b"\0")
        with member.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        after = member.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RecoveryError("checkpoint_changed_during_check")
        total += before.st_size
        count += 1
    if path.is_dir() and (paths != sorted(path.rglob("*")) or initial.st_mtime_ns != path.stat().st_mtime_ns):
        raise RecoveryError("checkpoint_changed_during_check")
    for member, before in before_stats.items():
        after = member.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino):
            raise RecoveryError("checkpoint_changed_during_check")
    return {"path": str(path), "sha256": digest.hexdigest(), "size_bytes": total, "files": count}


def matches(saved):
    if not isinstance(saved, dict) or not saved.get("sha256"):
        return False
    try:
        current = fingerprint(saved["path"])
    except (OSError, RecoveryError):
        return False
    return all(current[key] == saved.get(key) for key in ("sha256", "size_bytes", "files"))


def _safe_arguments(boundary, name, arguments):
    """Every saved string is opaque to the provider, including CLI tokens."""
    from .tools import ALL_TOOLS
    tool = next((t for t in ALL_TOOLS if t.name == name), None)
    if tool is None or set(arguments) - set(tool.input_schema):
        raise RecoveryError("unsupported_saved_arguments")
    def encode(value):
        if isinstance(value, str):
            if not value:
                return ""
            return boundary.alias(value, "param")
        if isinstance(value, list):
            return [encode(item) for item in value]
        if type(value) in (int, float, bool) or value is None:
            return value
        raise RecoveryError("unsupported_saved_arguments")
    safe = {key: encode(value) for key, value in arguments.items()}
    for key in ("input_path", "html_path", "output", "output_dir"):
        if arguments.get(key):
            safe[key] = boundary.register_path(arguments[key])
    return safe


def prepare(boundary, record):
    output = None
    name, status = record.get("tool"), record.get("status")
    if name not in RECOVERABLE or status not in {"completed", "started", "error", "interrupted"}:
        raise RecoveryError("recovery_not_supported_for_this_step")
    if status == "started" and record.get("owner_pid"):
        try:
            os.kill(record["owner_pid"], 0)
        except ProcessLookupError:
            pass
        except (PermissionError, TypeError):
            raise RecoveryError("original_process_may_still_be_running")
        else:
            raise RecoveryError("original_process_may_still_be_running")

    if status == "completed":
        checkpoints = record.get("output_fingerprints", [])
        if len(checkpoints) != 1 or Path(checkpoints[0]["path"]).suffix.lower() not in {".h5ad", ".zarr"}:
            raise RecoveryError("completed_step_has_no_dataset_checkpoint")
        if not matches(checkpoints[0]) or checkpoints[0]["size_bytes"] == 0:
            raise RecoveryError("checkpoint_changed_or_unverified")
        from . import commands
        checked = commands.run_readiness(checkpoints[0]["path"], str(boundary.output_root))
        match = re.search(r"(?m)^READINESS_JSON (\{.*\})$", checked.stdout)
        if not checked.ok or not match or json.loads(match[1]).get("ready") is not True:
            raise RecoveryError("checkpoint_not_ready")
        alias = boundary.register_path(checkpoints[0]["path"])
        text = (f"Resume building a KaroSpace viewer from verified local checkpoint {alias}. "
                f"The {name} step completed and its dataset has been verified unchanged. "
                "Reuse this output; do not repeat that completed step. Inspect its schema and run "
                "check_readiness before further processing, then continue the remaining build steps. "
                "No previous chat was restored; ask me about any missing scientific or display choices.")
    else:
        arguments = record.get("arguments", {})
        inputs, _ = io_paths(name, arguments)
        saved = record.get("input_fingerprints", [])
        if (len(inputs) != len(saved) or not inputs
                or not all(str(Path(path).expanduser().absolute()) == item.get("path") and matches(item)
                           for path, item in zip(inputs, saved))):
            raise RecoveryError("inputs_changed_or_unverified")
        arguments = json.loads(json.dumps(arguments))
        if name == "run_companion":
            tokens = arguments["args"]
            # Only a canonical prepare invocation with one explicit output is
            # safe to rewrite. In-place companion runs need manual review.
            positions = [i for i, token in enumerate(tokens) if token == "--output"]
            if len(positions) != 1 or positions[0] + 1 >= len(tokens) or any(t.startswith("--output=") for t in tokens):
                raise RecoveryError("retry_requires_explicit_output")
            output = boundary.output_root / ("recovery-" + uuid.uuid4().hex) / "prepared.h5ad"
            tokens[positions[0] + 1] = str(output)
        else:
            old_output = arguments.get("output")
            if not old_output:
                raise RecoveryError("retry_requires_explicit_output")
            if any(t == "-o" or t.startswith(("--output", "-o=")) for t in arguments.get("flags", [])):
                raise RecoveryError("retry_has_conflicting_output_flags")
            suffix = Path(old_output).suffix.lower()
            if suffix not in {".h5ad", ".html", ".ipynb", ".zarr"}:
                raise RecoveryError("unsupported_checkpoint")
            output = boundary.output_root / ("recovery-" + uuid.uuid4().hex) / ("result" + suffix)
            arguments["output"] = str(output)
        output.parent.mkdir(parents=True, exist_ok=False)
        safe = _safe_arguments(boundary, name, arguments)
        text = (f"Resume the interrupted KaroSpace workflow. The inputs for {name} were verified unchanged. "
                "Retry this step using these exact saved arguments and a fresh output location: "
                + json.dumps({"tool": name, "arguments": safe}) + ". "
                "Run check_readiness on the input before expensive processing if it is an assembled dataset. "
                "Preserve previous partial outputs. After success, inspect the new output and continue "
                "the remaining build steps. Ask me about choices not present in these saved parameters.")
    draft = boundary.preview(text)
    boundary._recovery_checks[draft["draft_id"]] = (record, str(output) if output is not None else None)
    # Match the boundary's bounded preview lifetime.
    boundary._recovery_checks = {key: value for key, value in boundary._recovery_checks.items() if key in boundary._drafts}
    return draft


def revalidate(boundary, token):
    """Recheck the files when the researcher approves, not only at preview time."""
    record, output = boundary._recovery_checks.pop(token)
    field = "output_fingerprints" if record["status"] == "completed" else "input_fingerprints"
    if not record.get(field) or not all(matches(saved) for saved in record[field]):
        boundary._drafts.pop(token, None)
        raise RecoveryError("files_changed_since_recovery_preview")
    if output and (Path(output).exists() or Path(output).is_symlink()):
        boundary._drafts.pop(token, None)
        raise RecoveryError("retry_output_already_exists")
