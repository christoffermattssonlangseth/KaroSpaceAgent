"""Local aliases and an allowlist of data that may cross the model boundary.

Raw subprocess output is never redacted-and-forwarded. We construct a new
response from recognized schema fields, fixed diagnostic codes and file stats.
Unrecognized formats fail closed. Alias maps live only in this process.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import uuid

from . import commands
from .sanitize import stat_path

PRIVACY_INSTRUCTIONS = """
Local privacy boundary: file paths and schema names are opaque aliases. Use
the aliases exactly in tool arguments; never infer or ask for their real names.
Use role_hints to choose columns. Write generated artifacts below /karo/output/.
Tool responses contain only structured schema, status and fixed diagnostics.
Raw logs and errors stay local. If a response says schema_unavailable, explain
that local inspection needs attention; do not request raw data or logs in chat.
"""


def result(data: dict, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data)}], "is_error": error}


def _number(value: str) -> int:
    return int(value.replace(",", ""))


def _as_list(value) -> list:
    """Normalize a jsonlite field to a list. auto_unbox=TRUE collapses a length-1
    R vector to a scalar and drops an absent field to None, so a name list can
    arrive as None, a scalar, or a list — treat all three uniformly."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class ApprovedMessage(str):
    """One-use message minted only after a local draft is explicitly approved."""
    def __new__(cls, text, token):
        obj = super().__new__(cls, text)
        obj.token = token
        return obj


def _hints(name: str) -> list[str]:
    """Return only fixed vocabulary, never a substring of the input name."""
    lower = name.lower()
    groups = {
        "cell_annotation": ("cell_type", "celltype", "annotation", "leiden", "louvain", "cluster", "niche", "domain", "cellcharter", "banksy"),
        "section": ("sample", "section", "slide", "library", "fov", "orig.ident"),
        "replicate": ("patient", "subject", "animal", "donor", "replicate", "sample_batch"),
        "condition": ("condition", "treatment", "stage", "genotype", "timepoint", "sex", "region"),
        "identifier": ("cell_id", "barcode", "patient", "subject", "donor", "identifier"),
        "quality_control": ("n_counts", "n_genes", "total_counts", "pct_counts", "area", "quality"),
        "counts": ("counts", "raw"), "normalized": ("normalized", "lognorm", "logcounts"),
        "spatial": ("spatial",), "umap": ("umap",), "pca": ("pca",),
        "derived": ("karospace_", "x_karo_", "polygon_index"),
        "spatial_x": ("x_centroid", "center_x", "array_col", "pxl_col"),
        "spatial_y": ("y_centroid", "center_y", "array_row", "pxl_row"),
    }
    return [role for role, needles in groups.items() if any(n in lower for n in needles)]


class Boundary:
    def __init__(self, allow_local_paths: bool = False):
        self.allow_local_paths = allow_local_paths
        self.aliases: dict[str, str] = {}
        self._reverse: dict[tuple[str, str], str] = {}
        self.output_root = commands.REPO_ROOT / "output" / uuid.uuid4().hex
        self._drafts = {}
        self._approved = {}
        self.local_reports = []

    def preview(self, text: str) -> dict:
        token = uuid.uuid4().hex
        prepared = self.prepare_message(text)
        self._drafts[token] = prepared
        if len(self._drafts) > 64:
            self._drafts.pop(next(iter(self._drafts)))
        return {"draft_id": token, "text": prepared}

    def approve(self, token: str) -> ApprovedMessage:
        if token not in self._drafts:
            raise ValueError("Preview expired. Prepare a new preview.")
        text = self._drafts.pop(token)
        self._approved[token] = text
        return ApprovedMessage(text, token)

    def consume(self, message: ApprovedMessage) -> str:
        if not isinstance(message, ApprovedMessage):
            raise ValueError("A local privacy preview is required before sending text to the model.")
        text = self._approved.pop(message.token, None)
        if text is None or text != str(message):
            raise ValueError("Message approval is invalid or has already been used.")
        return text

    def display(self, text: str) -> str:
        for alias, local in sorted(self.aliases.items(), key=lambda item: len(item[0]), reverse=True):
            text = text.replace(alias, local)
        return text.replace("/karo/output/", str(self.output_root) + "/")

    def alias(self, value: str, kind: str) -> str:
        key = (kind, value)
        if key not in self._reverse:
            count = sum(k[0] == kind for k in self._reverse) + 1
            token = f"{kind}_{count}"
            if kind == "file":
                suffix = Path(value).suffix.lower()
                suffix = suffix if suffix in {".h5ad", ".zarr", ".rds", ".rdata", ".html", ".json", ".karospace", ".ipynb"} else ""
                token = f"/karo/files/{token}{suffix}"
            self._reverse[key] = token
            self.aliases[token] = value
        return self._reverse[key]

    def register_path(self, path: str) -> str:
        return self.alias(str(Path(path).expanduser().absolute()), "file")

    def prepare_message(self, text: str) -> str:
        # The CLI's opening prompt has an unambiguous local-only path field.
        text = re.sub(r"(?m)^Input file: (.+)$", lambda m: "Input file: " + self.register_path(m[1]), text)
        # Quoted paths may contain spaces. Remaining paths must be single tokens;
        # the mandatory preview lets the researcher edit any free text left over.
        text = re.sub(r"([\"'])([~/][^\n]*?)\1", lambda m: self.register_path(m[2]) if not m[2].startswith('/karo/') else m[0], text)
        text = re.sub(r"(?<![\w:/])(?:~/|/)(?!karo/)[^\s\"'<>]+", lambda m: self.register_path(m[0]), text)
        known = {value: alias for alias, value in self.aliases.items() if value}
        if known:
            pattern = r"(?<!\w)(?:" + "|".join(re.escape(v) for v in sorted(known, key=len, reverse=True)) + r")(?!\w)"
            text = re.sub(pattern, lambda m: known[m[0]], text)
        return text

    def _path(self, value: str, output=False) -> str:
        if value in self.aliases and value.startswith("/karo/files/"):
            return self.aliases[value]
        prefix = "/karo/output/"
        if value.startswith(prefix):
            relative = Path(value[len(prefix):])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("invalid_path_alias")
            path = self.output_root / relative
            if output:
                path.parent.mkdir(parents=True, exist_ok=True)
            return str(path)
        raise ValueError("unregistered_path")

    def decode(self, arguments: dict) -> dict:
        def names(value):
            if isinstance(value, str):
                # Aliases can occur inside comma-separated column lists and
                # companion argv. Boundaries prevent col_1 matching col_10.
                for alias, original in self.aliases.items():
                    value = re.sub(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", lambda _: original, value)
                if value.startswith("/karo/output/"):
                    return self._path(value, output=True)
                return value
            if isinstance(value, list):
                return [names(v) for v in value]
            return value

        decoded = {k: names(v) for k, v in arguments.items()}
        for key in ("input_path", "html_path", "output", "output_dir"):
            if arguments.get(key):
                decoded[key] = self._path(arguments[key], output=key in {"output", "output_dir"})
        if "paths" in arguments:
            decoded["paths"] = [self._path(v) for v in arguments["paths"]]
        return decoded

    async def invoke(self, tool, arguments: dict) -> dict:
        try:
            if self.allow_local_paths:
                arguments = dict(arguments)
                for key in ("input_path", "html_path", "output", "output_dir"):
                    if arguments.get(key) and not arguments[key].startswith("/karo/"):
                        arguments[key] = self.register_path(arguments[key])
                if "paths" in arguments:
                    arguments["paths"] = [v if v.startswith("/karo/") else self.register_path(v)
                                          for v in arguments["paths"]]
            local_args = self.decode(arguments)
            raw = await tool.handler(local_args)
            return self.filter(tool.name, arguments, local_args, raw)
        except Exception:
            # Never forward exception strings: validators, path libraries and
            # third-party code frequently include the offending private value.
            return result({"status": "error", "diagnostic": "local_tool_error",
                           "message": "Check the local input and registered aliases. Details withheld."}, True)

    def filter(self, name: str, remote_args: dict, local_args: dict, raw: dict) -> dict:
        try:
            return self._filter(name, remote_args, local_args, raw)
        except Exception:
            return result({"status": "error", "diagnostic": "schema_unavailable"}, True)

    def _filter(self, name: str, remote_args: dict, local_args: dict, raw: dict) -> dict:
        info = raw.get("_local", {})
        if info:
            self.local_reports.append(info)
            self.local_reports[:] = self.local_reports[-10:]
        code = info.get("returncode", 1 if raw.get("is_error") else 0)
        stdout = info.get("stdout", "")
        if code or raw.get("is_error"):
            text = (stdout + "\n" + info.get("stderr", "")).lower()
            diagnostic = "operation_failed"
            for needle, fixed in (
                ("not found on path", "executable_missing"),
                ("executable not found", "executable_missing"),
                ("no spatial coordinates", "spatial_coordinates_missing"),
                ("--overwrite-derived", "existing_derived_outputs"),
                ("section key column", "invalid_section_key"),
                ("unrecognized arguments", "unsupported_arguments"),
                ("timed out", "timeout"),
                ("pseudobulk_replicate_required", "pseudobulk_replicate_required"),
                ("pseudobulk_piece_replicate", "pseudobulk_piece_replicate"),
                ("split_input_unsupported", "split_input_unsupported"),
                ("split_invalid_coordinates", "split_invalid_coordinates"),
                ("split_existing_column", "split_existing_column"),
                ("split_density_too_high", "split_density_too_high"),
                ("ingest_input_unsupported", "ingest_input_unsupported"),
                ("ingest_no_bundles", "ingest_no_bundles"),
                ("ingest_output_exists", "ingest_output_exists"),
                ("qc_input_unsupported", "qc_input_unsupported"),
                ("qc_no_threshold", "qc_no_threshold"),
                ("qc_counts_required", "qc_counts_required"),
                ("qc_output_same_as_input", "qc_output_same_as_input"),
                ("qc_output_exists", "qc_output_exists"),
            ):
                if needle in text:
                    diagnostic = fixed
                    break
            return result({"status": "error", "diagnostic": diagnostic,
                           "exit_code": int(code), "details": "Raw logs remain local."}, True)
        data = {"status": "ok", "exit_code": 0}
        if name == "inspect_input":
            columns = []
            for line in stdout.splitlines():
                match = re.match(r"\s*- (.+) \[(categorical|numeric|boolean|text|datetime|string|object); ([\d,]+) values(?:; ([\d,]+) missing)?\](?: examples:.*)?$", line)
                if match:
                    columns.append({"name": self.alias(match[1], "col"), "type": match[2],
                                    "cardinality": _number(match[3]), "missing": _number(match[4] or "0"),
                                    "role_hints": _hints(match[1])})
            if not columns and "Available cell metadata (adata.obs):" not in stdout:
                raise ValueError("unknown inspect format")
            data["columns"] = columns
            for field, label in (("cells", "Cells"), ("features", "Features")):
                match = re.search(r"(?m)^" + label + r": ([\d,]+)$", stdout)
                if match:
                    data[field] = _number(match[1])
            match = re.search(r"(?m)^SpatialData table: (.+)$", stdout)
            if match:
                data["table"] = self.alias(match[1], "table")
            data["modalities"] = []
            for match in re.finditer(r"(?m)^  - (.+) \[([^\n]+)\]( \(default\))?: ([\d,]+) features$", stdout):
                data["modalities"].append({"name": self.alias(match[1], "modality"),
                                           "features": _number(match[4]), "default": bool(match[3])})
        elif name == "inspect_structure":
            matrix = r"dtype=(bool|(?:u?int|float|complex)\d+), format=(dense|csr|csc|coo), all_integer=(yes|no|unknown)"
            for key, label in (("X", "X"), ("raw_X", "raw.X")):
                match = re.search(r"(?m)^" + re.escape(label) + r": " + matrix + "$", stdout)
                if match:
                    data[key] = dict(zip(("dtype", "format", "all_integer"), match.groups()))
            data["layers"] = []
            for match in re.finditer(r"(?m)^  - (.+): " + matrix + "$", stdout):
                data["layers"].append({"name": self.alias(match[1], "layer"), "role_hints": _hints(match[1]),
                                       **dict(zip(("dtype", "format", "all_integer"), match.groups()[1:]))})
            match = re.search(r"(?m)^obsm: (.+)$", stdout)
            data["embeddings"] = []
            if match and match[1] != "(none)":
                for entry in match[1].split(", "):
                    item = re.fullmatch(r"(.+) \((\d+|dataframe) cols\)", entry)
                    if not item:
                        raise ValueError("unknown embedding format")
                    data["embeddings"].append({"name": self.alias(item[1], "embedding"),
                                               "columns": int(item[2]) if item[2].isdigit() else "dataframe",
                                               "role_hints": _hints(item[1])})
            match = re.search(r"(?m)^spatial_graph_present: (yes|no)$", stdout)
            if not match:
                raise ValueError("unknown structure format")
            data["spatial_graph_present"] = match[1] == "yes"
        elif name == "validate_output":
            data["artifacts"] = []
            for token, path in zip(remote_args["paths"], local_args["paths"]):
                stat = stat_path(path)
                data["artifacts"].append({"path": token, "exists": stat.exists,
                                          "kind": stat.kind, "size_bytes": stat.size_bytes})
        elif name == "cli_help":
            # Help is invoked without a dataset and only with a fixed verb.
            # Only publish option names documented in our trusted playbook.
            # Even option-shaped strings in unexpected logs may identify people.
            from .prompt import SYSTEM_PROMPT
            documented = set(re.findall(r"--[a-z][a-z0-9-]*", SYSTEM_PROMPT)) | {"--help"}
            text = stdout or "\n".join(b.get("text", "") for b in raw.get("content", []))
            data["options"] = sorted(set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", text)) & documented)
        elif name in {"rds_inspect", "rds_convert"}:
            schema = json.loads(stdout)
            if not isinstance(schema, dict) or not any(k in schema for k in ("cells", "available_layers", "available_assays")):
                raise ValueError("unknown R schema")
            for key in ("cells", "genes", "has_spatial"):
                if type(schema.get(key)) in (int, bool):
                    data[key] = schema[key]
            for key, kind in (("available_assays", "assay"), ("available_layers", "layer"), ("reduced_dims", "embedding")):
                if isinstance(schema.get(key), list):
                    data[key] = [{"name": self.alias(v, kind), "role_hints": _hints(v)}
                                 for v in schema[key] if isinstance(v, str)]
            for key, kind in (("selected_assay", "assay"), ("x_name", "layer")):
                if isinstance(schema.get(key), str):
                    data[key] = self.alias(schema[key], kind)
        elif name == "rds_validate":
            # Schema-only read-back of a written .h5ad. jsonlite auto_unbox turns a
            # length-1 vector into a scalar, so coerce every name field to a list.
            # NAMES and COUNTS only; the local `input` path is never forwarded.
            schema = json.loads(stdout)
            if not isinstance(schema, dict) or type(schema.get("cells")) is not int:
                raise ValueError("unknown validate schema")
            for key in ("cells", "genes"):
                if type(schema.get(key)) is int:
                    data[key] = schema[key]
            dims_by_name = {}
            for entry in _as_list(schema.get("assay_dims")):
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    dims = entry.get("dims")
                    dims_by_name[entry["name"]] = [int(d) for d in dims] if isinstance(dims, list) else None
            data["assays"] = []
            for a in _as_list(schema.get("assays")):
                if isinstance(a, str):
                    item = {"name": self.alias(a, "assay")}
                    if dims_by_name.get(a):
                        item["dims"] = dims_by_name[a]
                    data["assays"].append(item)
            reduced = [r for r in _as_list(schema.get("reduced_dims")) if isinstance(r, str)]
            data["reduced_dims"] = [{"name": self.alias(r, "embedding"), "role_hints": _hints(r)}
                                    for r in reduced]
            for key, kind in (("obs_columns", "col"), ("var_columns", "col")):
                data[key] = [{"name": self.alias(c, kind), "role_hints": _hints(c)}
                             for c in _as_list(schema.get(key)) if isinstance(c, str)]
            data["has_spatial"] = "spatial" in reduced
            if isinstance(schema.get("spatial_dims"), list):
                data["spatial_dims"] = [int(d) for d in schema["spatial_dims"]]
        elif name == "geo_manifest":
            data["samples"] = []
            sample = None
            for line in stdout.splitlines():
                match = re.match(r"^(GSM\d+)  \[(xenium|visium|visium_hd|merscope|chromium|unknown)\]", line)
                if match:
                    sample = {"accession": match[1], "platform": match[2], "files": []}
                    data["samples"].append(sample)
                elif line.startswith("GSM"):
                    sample = None
                match = re.fullmatch(r"    - (.+)  \([^\n]+\)", line)
                if match and sample is not None:
                    suffix = Path(match[1]).suffix.lower()
                    sample["files"].append({"match": self.alias(match[1], "filematch"),
                                             "kind": suffix if suffix in {".rds", ".rdata", ".zip", ".h5", ".gz", ".csv", ".parquet"} else "other"})
            if not data["samples"]:
                raise ValueError("unknown GEO schema")
        elif name == "geo_fetch_file":
            match = re.search(r"(?m)^fetched: (.+)$", stdout)
            if not match:
                raise ValueError("unknown fetch result")
            data["file"] = self.register_path(match[1])
        elif name == "split_sections":
            # Aggregate counts only: piece counts and per-piece cell counts. The
            # group VALUES (sample IDs) and every coordinate stay local.
            match = re.search(r"(?m)^SPLIT_SECTIONS_JSON (\{.*\})$", stdout)
            if not match:
                raise ValueError("unknown split result")
            summary = json.loads(match[1])
            if not isinstance(summary, dict):
                raise ValueError("invalid split summary")
            for field in ("n_sections", "n_groups"):
                if type(summary.get(field)) is not int or summary[field] < 1:
                    raise ValueError("invalid split count")
                data[field] = summary[field]
            if summary.get("method") not in ("auto", "kmeans") or not isinstance(summary.get("key"), str) or not summary["key"]:
                raise ValueError("invalid split schema")
            data["method"] = summary["method"]
            data["key"] = self.alias(summary["key"], "col")
            data["requires_local_review"] = True
            data["biological_replicates"] = False
            groups = summary.get("groups")
            if not isinstance(groups, list) or len(groups) != data["n_groups"]:
                raise ValueError("invalid split groups")
            data["groups"] = []
            for group in groups:
                if not isinstance(group, dict) or type(group.get("pieces")) is not int or group["pieces"] < 1:
                    raise ValueError("invalid piece count")
                sizes = group.get("sizes")
                if not isinstance(sizes, list) or len(sizes) != group["pieces"] or any(type(s) is not int or s < 1 for s in sizes):
                    raise ValueError("invalid piece sizes")
                data["groups"].append({"pieces": group["pieces"], "sizes": sizes})
            if sum(g["pieces"] for g in data["groups"]) != data["n_sections"]:
                raise ValueError("inconsistent split count")
        elif name == "preview_sections":
            # Aggregate proposed-piece counts ONLY. The panels themselves are
            # rendered and shown locally over the progress channel; no image, no
            # path, no coordinate, and no group VALUE ever reaches the model.
            match = re.search(r"(?m)^PREVIEW_SECTIONS_JSON (\{.*\})$", stdout)
            if not match:
                raise ValueError("unknown preview result")
            summary = json.loads(match[1])
            if not isinstance(summary, dict):
                raise ValueError("invalid preview summary")
            for field in ("n_groups", "n_panels"):
                if type(summary.get(field)) is not int or summary[field] < 1:
                    raise ValueError("invalid preview count")
                data[field] = summary[field]
            if summary.get("method") not in ("auto", "kmeans"):
                raise ValueError("invalid preview schema")
            data["method"] = summary["method"]
            data["requires_local_review"] = True
            groups = summary.get("groups")
            if not isinstance(groups, list) or len(groups) != data["n_groups"]:
                raise ValueError("invalid preview groups")
            data["groups"] = []
            for group in groups:
                if not isinstance(group, dict) or type(group.get("pieces")) is not int or group["pieces"] < 1:
                    raise ValueError("invalid piece count")
                data["groups"].append({"pieces": group["pieces"]})
        elif name == "ingest_xenium":
            # Aggregate counts only: how many bundles/cells/genes, and per-sample
            # cell counts. The bundle folder names became sample_id VALUES in the
            # written file and never cross; no path or coordinate crosses either.
            match = re.search(r"(?m)^INGEST_XENIUM_JSON (\{.*\})$", stdout)
            if not match:
                raise ValueError("unknown ingest result")
            summary = json.loads(match[1])
            if not isinstance(summary, dict):
                raise ValueError("invalid ingest summary")
            for field in ("n_samples", "n_cells", "n_genes"):
                if type(summary.get(field)) is not int or summary[field] < 1:
                    raise ValueError("invalid ingest count")
                data[field] = summary[field]
            data["controls_dropped"] = bool(summary.get("controls_dropped"))
            sizes = summary.get("sample_sizes")
            if (not isinstance(sizes, list) or len(sizes) != data["n_samples"]
                    or any(type(s) is not int or s < 1 for s in sizes)):
                raise ValueError("invalid sample sizes")
            if sum(sizes) != data["n_cells"] and summary.get("qc") is None:
                raise ValueError("inconsistent ingest count")
            data["sample_sizes"] = sizes
            qc = summary.get("qc")
            if qc is not None:
                if not isinstance(qc, dict) or any(type(qc.get(k)) is not int for k in ("n_before", "n_after")):
                    raise ValueError("invalid ingest qc")
                data["qc"] = {"n_before": qc["n_before"], "n_after": qc["n_after"]}
        elif name == "qc_filter":
            # Aggregate before/after cell counts ONLY; no per-cell value crosses.
            match = re.search(r"(?m)^QC_FILTER_JSON (\{.*\})$", stdout)
            if not match:
                raise ValueError("unknown qc result")
            summary = json.loads(match[1])
            if not isinstance(summary, dict):
                raise ValueError("invalid qc summary")
            for field in ("n_before", "n_after", "n_removed"):
                if type(summary.get(field)) is not int or summary[field] < 0:
                    raise ValueError("invalid qc count")
                data[field] = summary[field]
            if data["n_after"] > data["n_before"] or data["n_before"] - data["n_after"] != data["n_removed"]:
                raise ValueError("inconsistent qc count")
            for field in ("min_counts", "min_genes"):
                if type(summary.get(field)) is int:
                    data[field] = summary[field]
        if remote_args.get("output"):
            data["output"] = remote_args["output"]
        if remote_args.get("output_dir"):
            data["output_dir"] = remote_args["output_dir"]
        return result(data)
