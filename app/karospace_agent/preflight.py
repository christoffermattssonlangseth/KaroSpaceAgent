"""Operation-specific local readiness enforcement for the registered tools."""
from pathlib import Path

from . import commands


GUARDED_TOOLS = {"qc_filter", "run_preprocess", "add_umap", "split_sections",
                 "preview_sections", "run_companion", "run_export"}


def check(name, arguments):
    """Return a blocking report, or None when it is safe to start the operation.

    This always runs a fresh scan. A previous chat message or model-provided
    ready=true cannot authorize execution or bypass a changed input.
    """
    if name not in GUARDED_TOOLS:
        return None
    args = arguments
    options = {"require_spatial": name in {"split_sections", "preview_sections", "run_companion", "run_export"},
               "require_counts": name == "qc_filter"}
    source = args.get("input_path")
    output = args.get("output") or args.get("output_dir")
    if name in {"split_sections", "preview_sections"}:
        options.update(section_key=args.get("within") or "", coords_key=args.get("coords_key") or "spatial")
    if name in {"run_export", "run_companion"}:
        flags = args.get("flags", []) if name == "run_export" else args.get("args", [])
        if not isinstance(flags, list) or any(type(value) is not str for value in flags):
            return commands.RunResult(2, "", "readiness_arguments_unsupported")
        # argparse accepts abbreviated long flags; don't let them change the
        # operation's checked inputs or output after the probe has run.
        relevant = {"--output", "--groupby"} if name == "run_companion" else {
            "--output", "--section-key", "--spatial-key", "--spatial-x", "--spatial-y",
            "--spatialdata-table", "--statistics-counts-layer"}
        for token in flags:
            option = token.split("=", 1)[0]
            if option.startswith("--") and len(option) > 2 and any(
                    full.startswith(option) and full != option for full in relevant):
                return commands.RunResult(2, "", "readiness_arguments_unsupported")
        if name == "run_companion":
            if len(flags) < 2 or flags[0] != "prepare" or flags[1].startswith("-"):
                return commands.RunResult(2, "", "readiness_arguments_unsupported")
            source = flags[1]
            output = commands.flag_value(flags, "--output")
            if not output:
                return commands.RunResult(2, "", "readiness_explicit_output_required")
            if any(t.startswith("-o") and not t.startswith("--") for t in flags):
                return commands.RunResult(2, "", "readiness_conflicting_output")
            options["section_key"] = commands.flag_value(flags, "--groupby", "")
            options["coordinate_mode"] = "companion"
        else:
            # The tool's output argument must remain authoritative. Otherwise
            # later CLI tokens could redirect writes away from the checked path.
            if any(t.startswith("--output") or (t.startswith("-o") and not t.startswith("--")) for t in flags):
                return commands.RunResult(2, "", "readiness_conflicting_output")
            options.update(section_key=commands.flag_value(flags, "--section-key", "sample_id").strip(),
                           coordinate_mode="export",
                           coords_key=commands.flag_value(flags, "--spatial-key", "spatial"),
                           table=commands.flag_value(flags, "--spatialdata-table", ""),
                           spatial_x=commands.flag_value(flags, "--spatial-x", ""),
                           spatial_y=commands.flag_value(flags, "--spatial-y", ""))
            layer = commands.flag_value(flags, "--statistics-counts-layer", "")
            if layer:
                options.update(counts_layer=layer, require_counts=True)
    if not isinstance(source, str) or not source or not isinstance(output, str) or not output:
        return commands.RunResult(2, "", "readiness_arguments_unsupported")
    destination = output if name == "preview_sections" else str(Path(output).parent)
    checked = commands.run_readiness(source, destination, **options)
    if not checked.ok:
        return commands.RunResult(2, "", "readiness_check_unavailable")
    # Reuse the strict, typed privacy parser rather than trusting raw JSON.
    from .privacy import Boundary
    import json
    filtered = Boundary().filter("check_readiness", {}, {}, {"_local": {
        "returncode": checked.returncode, "stdout": checked.stdout, "stderr": checked.stderr}})
    if filtered.get("is_error"):
        return commands.RunResult(2, "", "readiness_check_unavailable")
    report = json.loads(filtered["content"][0]["text"])
    if not report.get("ready"):
        return commands.RunResult(2, "READINESS_JSON " + json.dumps(report), "readiness_blocked")
    # Resource estimates remain advisory; record them in the local progress
    # channel without introducing a model-controlled override for data errors.
    if report.get("warnings"):
        sink = commands.get_progress_sink()
        if sink is not None:
            try:
                sink("stderr", "Readiness warnings: " + ", ".join(report["warnings"]) + "\n")
            except Exception:
                pass  # A UI progress listener must not break execution.
    return None
