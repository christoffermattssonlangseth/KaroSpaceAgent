"""Local CPU model and registered-tool loop, used only by the confined launcher."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jsonschema

from . import commands, tools
from .history import RunHistory
from .offline_grammar import EnvelopeGrammar
from .privacy import Boundary
from .tool_worker import tool_specs


# Remote acquisition is unavailable even if a model invents its name. Remaining
# tools still run beneath OS network denial (including package dependencies).
OFFLINE_TOOLS = frozenset({"inspect_input", "inspect_structure", "check_readiness", "cli_help",
    "rds_inspect", "rds_convert", "rds_validate", "ingest_spatial", "qc_filter", "run_preprocess",
    "add_umap", "split_sections", "preview_sections", "generate_notebook", "merge_sections",
    "run_companion", "run_export", "package_sidecar", "validate_output"})
MAX_STEPS = 12

OFFLINE_HELP = (
    "I can help build a KaroSpace viewer from a local .h5ad or SpatialData .zarr, "
    "or convert supported R objects first. Local tools can inspect dataset schema, "
    "check readiness, filter QC, preprocess/cluster, add UMAP, split or merge sections, "
    "run the companion pipeline, export a viewer, package sidecars, and validate outputs. "
    "Some operations require their scientific dependencies to be installed.\n\n"
    "To start, choose your dataset when opening the app: I inspect its schema automatically "
    "before my first reply, so you can go straight to reviewing section, annotation and analysis "
    "choices before exporting. Use /inspect any time to re-run that schema inspection of the "
    "selected .h5ad/.zarr without model planning. "
    "Pasting a path does not grant access to an unselected file; reopen with Choose dataset if needed.\n\n"
    "Everything runs locally with network access blocked. Online downloads and cloud models "
    "are unavailable. A small CPU model can make planning mistakes; tool results and validation "
    "determine whether work succeeded. A larger local model (about 3-4B) is far more reliable "
    "than a 0.5B — see the README to install one. Use /tools to list the registered tools."
)


class LocalModel:
    def __init__(self, path):
        from .offline import model_path
        path = model_path(path)
        import mlx.core as mx
        # Set before importing mlx_lm: it creates a generation stream on import.
        # GPU compilation uses an external Mach service; CPU stays in process.
        mx.set_default_device(mx.cpu)
        # Eager CPU kernels avoid runtime compiler subprocesses and external
        # compiler/SDK helpers. No JIT code cache or GPU service is needed.
        mx.disable_compile()
        from mlx_lm import load
        self.model, self.tokenizer = load(str(path), tokenizer_config={
            "local_files_only": True, "trust_remote_code": False})
        # MLX's eager quantized CPU matmul is much slower than its dense CPU
        # kernel. Expand small models in memory; keep larger models quantized
        # so choosing one cannot silently multiply its RAM requirement.
        from mlx.utils import tree_flatten
        from mlx_lm.utils import dequantize_model
        parameters = sum(value.size for _, value in tree_flatten(self.model.parameters()))
        for _, module in self.model.named_modules():
            if hasattr(module, "bits") and hasattr(module, "weight"):
                parameters += module.weight.size * (32 / module.bits - 1)
        if parameters <= 600_000_000:
            self.model = dequantize_model(self.model)
            mx.eval(self.model.parameters())
        self._id_texts = None          # id -> its decoded piece (built once, lazily)
        self._structural_cache = {}    # charset -> [(id, piece)] usable in the skeleton

    def _structural_ids(self, charset):
        """Token ids whose whole decoded piece stays inside the grammar's skeleton
        charset — the only candidates worth scanning while the envelope is typed."""
        key = charset
        if key in self._structural_cache:
            return self._structural_cache[key]
        if self._id_texts is None:
            vocab = self.tokenizer.get_vocab()
            size = max(vocab.values()) + 1
            texts = []
            for i in range(size):
                try:
                    texts.append(self.tokenizer.decode([i]))
                except Exception:
                    texts.append("")
            self._id_texts = texts
        eos = set(self.tokenizer.eos_token_ids)
        usable = [(i, t) for i, t in enumerate(self._id_texts)
                  if t and i not in eos and all(c in charset for c in t)]
        self._structural_cache[key] = usable
        return usable

    def _envelope_processor(self, grammar, prompt_len):
        """An mlx logits processor that masks every token which would break the
        envelope grammar, releasing control once the free content region begins.
        Fails open: if a step has no legal token it leaves the logits untouched
        rather than making generation impossible."""
        import mlx.core as mx
        import numpy as np

        structural = self._structural_ids(grammar.charset)
        eos = list(self.tokenizer.eos_token_ids)

        def processor(tokens, logits):
            generated = tokens[prompt_len:].tolist()
            text = self.tokenizer.decode(generated) if generated else ""
            state = grammar.classify(text)
            if state == "released":
                return logits
            if state == "complete":
                allowed = eos
            else:  # typing, or a drifted "invalid" we recover from by failing open
                allowed = [i for i, piece in structural if grammar.classify(text + piece) != "invalid"]
                if not allowed:
                    return logits
            mask = np.full((logits.shape[-1],), -1e9, dtype=np.float32)
            mask[np.array(allowed, dtype=np.int64)] = 0.0
            return logits + mx.array(mask).astype(logits.dtype)[None, :]

        return processor

    def complete(self, messages, max_tokens=768, grammar=None):
        import mlx.core as mx
        mx.set_default_device(mx.cpu)
        from mlx_lm.generate import generate_step
        prompt = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        encoded = self.tokenizer.encode(prompt, add_special_tokens=False)
        if len(encoded) > 16384:
            raise ValueError("Local context is full. Start a new offline session using the saved output.")
        processors = [self._envelope_processor(grammar, len(encoded))] if grammar is not None else None
        # The high-level MLX generate() wrapper configures Metal wired memory,
        # even with a CPU default device. Use its public token generator directly.
        tokens = []
        for token, _ in generate_step(mx.array(encoded), self.model, max_tokens=max_tokens,
                                      prefill_step_size=256, logits_processors=processors):
            token = int(token)
            if token in self.tokenizer.eos_token_ids:
                break
            tokens.append(token)
        return self.tokenizer.decode(tokens)


def propose_candidates(columns):
    """Rank the two choices a small model most often gets wrong — the section/
    sample key and the main cell annotation — from filtered obs columns. Every
    field used here (alias name, role_hints, cardinality, type) has already
    crossed the boundary, so the hint carries no data values. Returns alias-only
    display strings; empty lists mean nothing obvious, so the model should ask."""
    def show(col):
        roles = ",".join(col.get("role_hints", [])) or "none"
        return f"{col.get('name')} (roles: {roles}; {col.get('cardinality', '?')} values)"

    sections, annotations = [], []
    for col in columns:
        hints = set(col.get("role_hints", []))
        card = col.get("cardinality") or 0
        categorical = col.get("type") in {"categorical", "boolean", "text", "str", "string"}
        if "section" in hints and card >= 2:
            sections.append((0, card, col))
        elif categorical and (hints & {"replicate", "condition"}) and 2 <= card <= 64:
            sections.append((1, card, col))
        if "cell_annotation" in hints:
            annotations.append((0 if 2 <= card <= 100 else 1, card, col))
    order = lambda item: (item[0], item[1])
    return {"section": [show(c) for _, _, c in sorted(sections, key=order)][:4],
            "annotation": [show(c) for _, _, c in sorted(annotations, key=order)][:4]}


def parse_action(text):
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    value = json.loads(text)
    if type(value) is not dict:
        raise ValueError("Expected a JSON object")
    if set(value) == {"message"} and type(value["message"]) is str:
        return value
    if set(value) == {"describe_tool"} and type(value["describe_tool"]) is str:
        return value
    if (set(value) == {"tool", "arguments"} and type(value["tool"]) is str
            and type(value["arguments"]) is dict):
        return value
    raise ValueError("Expected a message or registered tool action")


class OfflineSession:
    def __init__(self, config, on_event=None, on_progress=None, model_factory=LocalModel):
        self.config = config
        workspace = Path(config["workspace"])
        self.boundary = Boundary(history=RunHistory(provider="offline", model="local-mlx-cpu", root=workspace / "history"))
        self.boundary.output_root = workspace / "output"
        self.on_event = on_event or (lambda text: print(text, flush=True))
        self.on_progress = on_progress or (lambda stream, line: None)
        self.specs = {s["name"]: s for s in tool_specs() if s["name"] in OFFLINE_TOOLS}
        # Constrain generation to a valid envelope over exactly these tool names,
        # so malformed JSON and invented tool names can never be produced.
        self.grammar = EnvelopeGrammar(self.specs)
        # CPU inference benefits from progressive disclosure: only describe
        # tools being used, instead of repeatedly prefilling every schema.
        catalog = {name: spec["description"].split(". ", 1)[0] for name, spec in sorted(self.specs.items())}
        prompt = (
            "You are KaroSpace's fully local assistant. All tools and inference run locally. "
            "You CAN build KaroSpace spatial-transcriptomics viewers using the registered tools. "
            "When asked what you can do or what skills you have, explain these capabilities. "
            "For a viewer request without a dataset, ask the user to select a local dataset at app startup. "
            "For a selected .h5ad/.zarr, begin with inspect_input, then inspect_structure. "
            "For R objects, begin with rds_inspect and rds_convert. Review section and annotation choices "
            "with the user, check_readiness, verify flags with cli_help, run_export, then validate_output. "
            "If the selected dataset was already inspected (results appear above), use them and do not "
            "call inspect_input or inspect_structure again for it. "
            "There is no internet, cloud model or download capability. Never claim a tool succeeded "
            "without a successful tool result. Ask about scientific choices; do not invent annotations "
            "or biological replicates. Inspect schema first. Use add_umap for analyzed data missing UMAP; "
            "use run_preprocess only when clustering is intended. Use paths and schema aliases exactly. "
            "Never request raw dataset values in the chat. Exports stay on disk; do not open browsers. "
            "Answer the latest user request with exactly one JSON object. For a conversational answer, "
            "use the single key message with your actual answer as its string value. "
            "For a tool call, use keys tool (a registered name) and arguments (an object). "
            "To request a tool's schema first, use the single key describe_tool with the tool name. "
            "Do not repeat these instructions. No text outside the JSON object. "
            "Only the following tools are available:\n" + json.dumps(catalog)
        )
        self.messages = [
            {"role": "system", "content": prompt},
            # A complete conversational example helps small local models keep
            # messages separate from tool calls without copying placeholders.
            {"role": "user", "content": "Say hello without using any tools."},
            {"role": "assistant", "content": '{"message":"Hello."}'},
            {"role": "user", "content": "Can you make a KaroSpace viewer?"},
            {"role": "assistant", "content": '{"message":"Yes. Choose a local dataset when opening the app. I can inspect its schema, help review export options, run the KaroSpace export and validate the output locally."}'},
        ]
        # A selected dataset is inspected once, deterministically, before the
        # model's first planning turn (see _seed_inspection). This flag keeps it
        # from being re-inspected on later turns or after an explicit /inspect.
        self._seeded = False
        # State-machine rails: the set of tools that have succeeded drives the one
        # objective injected each turn, keeping a weak model from skipping the
        # export/validation or declaring the viewer done before it exists.
        self._done = set()
        self._last_objective = None
        self.model = model_factory(config["model"])

    def send(self, text):
        # Clicking Send in this local-only surface authorizes local processing.
        # Still keep the same path/schema aliases and tool result filters.
        # The web preview aliases slash commands as paths. Resolve locally only
        # to recognize fixed commands, never to expose raw paths.
        command = self.boundary.display(text).strip().lower()
        draft = self.boundary.preview(text)
        prepared = self.boundary.consume(self.boundary.approve(draft["draft_id"]))
        self.messages.append({"role": "user", "content": prepared})
        if command.rstrip("?.!") in {"/help", "/tools", "what can you do", "what skills do you have", "can you make a karospace viewer"}:
            self.messages[-1]["content"] = "/tools" if command == "/tools" else "/help"
            answer = OFFLINE_HELP
            if command == "/tools":
                answer += "\n\nRegistered local tools:\n" + "\n".join(sorted(self.specs))
            self.messages.append({"role": "assistant", "content": json.dumps({"message": answer})})
            self.on_event(answer)
            return answer
        if command == "/inspect":
            self.messages[-1]["content"] = "/inspect"
            return self.inspect_selected()
        commands.set_progress_sink(self.on_progress)
        try:
            self._seed_inspection()
            for _ in range(MAX_STEPS):
                # State-machine rails: pin the current objective for a dataset
                # build, re-issuing it only when a tool success has advanced it.
                if self._has_selected_dataset():
                    objective = self._objective()
                    if objective != self._last_objective:
                        self.messages.append({"role": "user", "content": objective})
                        self._last_objective = objective
                raw = self.model.complete(self.messages, grammar=self.grammar)
                self.messages.append({"role": "assistant", "content": raw})
                try:
                    action = parse_action(raw)
                except (ValueError, TypeError):
                    self.messages.append({"role": "user", "content": "Invalid response. Return exactly one JSON message or tool action."})
                    continue
                if "message" in action:
                    text = self.boundary.display(action["message"])
                    self.on_event(text)
                    return text
                if "describe_tool" in action:
                    name = action["describe_tool"]
                    specification = self.specs.get(name)
                    result = ({"name": name, "description": specification["description"],
                               "schema": specification["inputSchema"]} if specification else
                              {"error": "tool_unavailable_offline"})
                    self.messages.append({"role": "user", "content": "Local tool description: " + json.dumps(result)})
                    continue
                name, arguments = action["tool"], action["arguments"]
                if name not in self.specs:
                    result = {"error": "tool_unavailable_offline"}
                else:
                    try:
                        jsonschema.validate(arguments, self.specs[name]["inputSchema"])
                    except jsonschema.ValidationError:
                        result = {"error": "invalid_tool_arguments", "schema": self.specs[name]["inputSchema"]}
                    else:
                        self.on_progress("status", f"Running {name} locally.\n")
                        definition = next(t for t in tools.ALL_TOOLS if t.name == name)
                        result = asyncio.run(self.boundary.invoke(definition, arguments))
                        if not result.get("is_error"):
                            self._done.add(name)  # advances the objective (state machine)
                self.messages.append({"role": "user", "content": "Local tool result: " + json.dumps(result)})
            raise RuntimeError("The local model reached its tool-step limit. Review the local history before continuing.")
        finally:
            commands.set_progress_sink(None)

    def _has_selected_dataset(self):
        selected = self.config.get("input_path")
        return bool(selected) and Path(selected).suffix.lower() in {".h5ad", ".zarr"}

    def _objective(self):
        """The single next goal, derived from which tools have already succeeded.

        A weak model left to free-plan across MAX_STEPS drifts: it invents an
        order, skips validation, or declares the viewer done before it exists.
        Injecting one deterministic objective each turn — advanced only by real
        tool successes (self._done) — turns the loop into a small state machine
        (inspect → confirm columns → export → validate → report) without a rigid
        dispatcher, so ordinary chat and tool use still flow through the model."""
        if "run_export" not in self._done:
            return ("Objective: confirm the section/sample key and the main cell annotation with the "
                    "user (use the candidate hints; do not invent columns), then run check_readiness, "
                    "verify flags with cli_help, and call run_export. Do not tell the user the viewer "
                    "is finished before run_export has succeeded.")
        if "validate_output" not in self._done:
            return ("Objective: run_export succeeded. Call validate_output on the exported path before "
                    "you report the result. Do not claim success until validation passes.")
        return ("Objective: export and validation both succeeded. You may now report completion to the "
                "user with a message.")

    def _run_inspection(self):
        """Run the two schema-only inspections on the selected dataset, appending
        each filtered result to the local message history. Returns (ok, reports,
        columns) — display-safe text blocks plus the parsed obs columns used for
        candidate hints. The caller owns the progress sink and marks the session
        seeded; only aggregate schema — never data values — is kept."""
        arguments = {"input_path": self.boundary.register_path(self.config["input_path"]), "spatialdata_table": ""}
        reports, ok, columns = [], True, []
        for name in ("inspect_input", "inspect_structure"):
            self.on_progress("status", f"Running {name} locally.\n")
            definition = next(t for t in tools.ALL_TOOLS if t.name == name)
            result = asyncio.run(self.boundary.invoke(definition, arguments))
            self.messages.append({"role": "user", "content": "Local tool result: " + json.dumps({"tool": name, **result})})
            if result.get("is_error"):
                ok = False
                reports.append("Inspection could not complete. Review the local History entry; no viewer was exported.")
                break
            blocks = [block["text"] for block in result.get("content", []) if block.get("type") == "text"]
            reports.append(name + ":\n" + "\n".join(blocks))
            if name == "inspect_input":
                for block in blocks:
                    try:
                        parsed = json.loads(block)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(parsed, dict) and isinstance(parsed.get("columns"), list):
                        columns = parsed["columns"]
        return ok, reports, columns

    def _seed_inspection(self):
        """Ground the model with a schema-only inspection of the selected dataset
        before its first planning turn. A small local model then never has to
        discover a file is present or call inspect_input itself — the failure mode
        where it just answers "Done." without doing any work. Runs at most once;
        the caller (send) already holds the progress sink."""
        if self._seeded:
            return
        self._seeded = True
        if not self._has_selected_dataset():
            return
        ok, _, columns = self._run_inspection()
        if not ok:
            self.messages.append({"role": "user", "content":
                "Automatic inspection of the selected dataset failed. Tell the user and do not "
                "claim any export or tool succeeded."})
            return
        note = ("The selected dataset was inspected automatically above (schema only, no data "
                "values). Use these results to plan; do not call inspect_input or "
                "inspect_structure again for this dataset.")
        candidates = propose_candidates(columns)
        if candidates["section"] or candidates["annotation"]:
            note += ("\nLocal candidate hints (ranked from the schema; confirm with the user, do not "
                     "invent columns). Section/sample key: "
                     + "; ".join(candidates["section"] or ["none obvious — ask the user"])
                     + ". Main cell annotation: "
                     + "; ".join(candidates["annotation"] or ["none obvious — ask the user"]) + ".")
        self.messages.append({"role": "user", "content": note})

    def inspect_selected(self):
        """A deterministic read-only entry point for the small CPU model's UI."""
        if not self._has_selected_dataset():
            answer = "Reopen the app and choose a local .h5ad or .zarr dataset to use /inspect."
        else:
            commands.set_progress_sink(self.on_progress)
            try:
                _, reports, _ = self._run_inspection()
            finally:
                commands.set_progress_sink(None)
            self._seeded = True  # the model already holds this dataset's schema
            answer = "\n\n".join(reports)
        self.messages.append({"role": "assistant", "content": json.dumps({"message": answer})})
        answer = self.boundary.display(answer)
        self.on_event(answer)
        return answer
