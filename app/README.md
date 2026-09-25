# `karospace-agent` — the standalone app

The `karospace-agent` app drives the local `karospace` CLI using Claude or Codex.
The same playbook also ships as repository skills. The standalone app keeps
computation local and exposes sanitizing tool handlers to either model backend.

- **local hands** — every side effect is a subprocess wrapper (`commands.py`);
- **model reasoning** — Claude or Codex chooses flags using `prompt.py`;
- **restricted tool replies** — `privacy.py` constructs fresh responses from
  recognized schema fields and fixed status codes. Paths and schema labels become
  local aliases; values, filenames, raw errors and logs are withheld. Unrecognized
  schema output fails closed.
- **local message review** — every outgoing message requires an explicit preview,
  including opening build requests. The app substitutes recognized paths and
  known schema labels. Review all remaining prose: arbitrary identifiers cannot
  be reliably detected automatically. Approving text containing a name or ID
  will send it. This is not an absolute anonymity or GDPR compliance guarantee.
- **isolated capabilities** — Claude disables built-ins; Codex has no execution
  environment and uses a temporary profile without personal instructions,
  skills, plugins, MCP settings or history. No raw-file reader is exposed.

## Install

```bash
cd app
pip install -e .          # pulls claude-agent-sdk
```

Requires `karospace` on PATH. The Rust companion is optional; the app finds it at
`../../KaroSpaceCompanion/target/release/karospace-companion` or via
`KAROSPACE_COMPANION`. For `.rds` inputs (Seurat / SingleCellExperiment), the
optional [`rds2h5ad`](https://github.com/christoffermattssonlangseth/RDStoH5AD)
converter is used if present — `pip install rdstoh5ad` (needs R with
`zellkonverter`), found on PATH or via `RDS2H5AD_BIN`.

Then `karospace-agent auth` — it reports which Claude credential the model will
run under (a Console sign-in via `claude` `/login`, an API key, federation, or a
cloud provider) and prints sign-in instructions if none is found. A claude.ai
subscription login is flagged as not permitted for this app; see the root README
→ *Authentication* for why. `auth.py` mirrors Claude Code's credential
precedence from the outside, reading env-var presence and file key names only,
never a secret value.

## Use

For the native chat window with Codex:

```bash
pip install -e '.[app]'
codex login
karospace-agent app --provider codex
```

Install Codex CLI with `npm install -g @openai/codex` if needed. The app uses
file-backed Codex credentials through app-server (tested with CLI 0.155.1),
including ChatGPT authentication. Only `CODEX_HOME/auth.json` is copied into a
private temporary profile and removed on close. Keyring-only login is not yet
supported; use `codex -c 'cli_auth_credentials_store="file"' login` if needed.
Sign in again if a copied credential expires or requires reauthentication. The dynamic-tool API is experimental. `--provider codex`
also works for `build`, `chat`, `web`, and `auth`; the default provider is Claude.
Choose a specific model with `--model`, or use the default reported by Codex's
model catalogue. `karospace-agent auth --provider codex` checks the login.

The window opens immediately; Codex connects on the first message. Replies,
tool calls, and local command progress use the existing UI. Stop interrupts
the turn and terminates its local tool worker and child processes on macOS/Linux.

```bash
karospace-agent build ~/data/my_xenium.h5ad \
  "grid by sample, colour by cell_type, focus on Cd4/Cd8a/Gfap"
```

The agent inspects the file, chooses flags, (optionally) runs the companion, runs
the export, uses fixed diagnostics to iterate, produces both the sidecar viewer and the
`.karospace` package, and validates the output — streaming progress as it goes.

For a multi-turn session — the agent can ask you questions and you can iterate
on the result — use `chat`, with or without an opening build:

```bash
karospace-agent chat ~/data/my_xenium.h5ad "grid by sample"
karospace-agent chat
```

Type `/quit` (or Ctrl-D) to leave; Ctrl-C mid-turn interrupts the model and
returns to the prompt (a second Ctrl-C quits). `chat` uses the SDK's `ClaudeSDKClient`
(one session, context kept across turns) with the *same* options as `build`:
built-ins off, the same sanitizing tools, and a short conversation addendum to the
system prompt. Before each turn, review the aliased text and type `SEND`.
Remove identifying prose; quote paths that contain spaces. Cancelled drafts
are not sent. Programmatic `agent.run()` callers must supply an async `review`
callback; `Session.send()` accepts only a one-use approval from its boundary.

### Tissue pieces, replicates, and notebook review

Section splitting is optional and currently supports `.h5ad` with coordinates in
`obsm`. The proposed pieces need local visual review; auto mode can merge real
pieces or absorb small pieces. Original columns and the source file are preserved.
Unsupported inputs keep the normal export route. Dense auto neighbourhoods stop
with `split_density_too_high` rather than attempting a large allocation; use a
locally reviewed smaller `--eps` in the script, or a confirmed count with k-means.

Pseudobulk exports require an explicit `--pseudobulk-replicate-annotation` naming
confirmed biological units. A local check rejects generated piece columns using
provenance in `uns['karospace_section_split']`. This does not infer whether an
arbitrary column really represents independent donors; that remains a scientific
choice. Older split files lack this provenance: select the original donor/sample
column explicitly or leave pseudobulk off.

Generated CellCharter notebooks use the documented `ClusterAutoK` stability
workflow, with local plots and no default primary domain count. Set `PRIMARY_K`
after review; the final write is blocked until the selection has been applied.
scVI selects its raw-count HVGs independently of Leiden (all genes for panels up
to 5,000 features). Checkpoints support nullable string metadata. The per-library
aggregation copies only latent vectors and the graph; training still needs enough
RAM/GPU memory and the installed CellCharter/scVI dependencies. See the notebook
for checkpoint recovery instructions.

### Raw spatial ingestion, QC, and R objects

Three tools bring raw inputs to the `.h5ad` the pipeline expects, all computing
locally and crossing only aggregate counts:

- `ingest_spatial` assembles a local folder of raw spatial bundles into one file
  with a per-cell `sample_id`, auto-detecting each bundle's vendor layout: Xenium
  (`cell_feature_matrix.h5` + `cells.parquet` / `cells.csv`) and MERSCOPE / MERFISH
  (Vizgen's `cell_by_gene.csv` + `cell_metadata.csv`); a folder mixing both is fine.
  It reuses the GEO builder's per-vendor assembly cores so ingested and GEO-built
  files are structurally identical. Control / blank probes drop by default;
  `--include-control` keeps them. Bundle paths (relative to the root) become
  `sample_id` values and stay local — only sample/cell/gene counts, per-sample
  sizes, and per-platform bundle counts cross.
- `qc_filter` drops low-quality cells by total counts and/or detected genes (both
  thresholds off unless set; ~40 counts / ~15 genes is a common starting point).
  It validates that `X` holds raw counts and rejects normalized input, forwarding
  only before/after cell counts.
- `rds_convert` / `rds_validate` convert a Seurat / SingleCellExperiment `.rds`
  via rds2h5ad and read the result back; only aliased names and counts cross,
  never the local path.

Both writers refuse to overwrite an existing output and publish atomically
(staging dir + hard link), so a partial or concurrent write never replaces a real
file; failures map to fixed diagnostics with raw logs kept local. Inspect any
written file with `inspect_input` / `inspect_structure` for its schema.

### Browser mode

```bash
pip install -e '.[web]'          # starlette + uvicorn
karospace-agent web              # or: karospace-agent web <input> "<intent>"
```

Opens one conversation at `http://127.0.0.1:8765/` (`--host`, `--port`). The
server owns a single `Session` and runs user turns one at a time; the page
follows a Server-Sent Events stream (`GET /events`, replayed on reconnect),
prepares local drafts through `/preview`, and submits only their reviewed
`draft_id` to `/send`. `/interrupt` stops the running turn. Opening prompts
remain local drafts until reviewed. Child-process
progress arrives on the same stream via the progress sink. Localhost only by
default and no auth: it is a local app with a browser window, not a service.

## Layout

| File | Role |
| --- | --- |
| `commands.py` | Subprocess wrappers; locates `karospace` / companion / merge script. No model contact. |
| `privacy.py` | Model-bound allowlist, local aliases, fixed diagnostics and one-use message review. |
| `history.py` | Private local tool-run records, command provenance, file metadata and software versions; never exposed through model tools. |
| `sanitize.py` | Local output formatting and path *stat* (never file bytes). |
| `tools.py` | The `@tool` local hands (inspect / acquire / .rds convert / prepare / enrich / export / deliver), wrapped by the privacy boundary before any model reply. |
| `prompt.py` | System prompt — the viewer-building playbook, ported to the tools. |
| `agent.py` | Provider selection and Claude options/session; one-shot builds. |
| `codex.py` | Codex app-server connection, conversation state, tool dispatch and interruption. |
| `tool_worker.py` | Validates and executes one existing tool in a cancellable local worker; the parent filters reports before they reach Codex. |
| `mcp.py` | Serves the same 22 sanitizing tools over stdio for Codex and other MCP clients; no model session or Claude authentication. |
| `auth.py` | Detects which credential the CLI subprocess will use and whether it is permitted; no secret is read. |
| `cli.py` | `karospace-agent build <input> "<intent>"`, `karospace-agent chat [input] ["<intent>"]` (the REPL), `karospace-agent web`, and `karospace-agent auth`. |
| `web.py` | The browser front end: Starlette app, SSE event hub, one-turn-at-a-time worker (optional `[web]` extra). |
| `static/index.html` | The single-file page `web.py` serves (inline SVG logo + favicon). |
| `static/appicon.png` | The Dock/app icon `desktop.py` sets at runtime for `karospace-agent app`. |

## Config

### Codex / standalone MCP

After installing the app, an MCP client can launch `karospace-agent-mcp` or
`python -m karospace_agent.mcp`. The repository's `.codex/config.toml` uses the
module form, so an existing editable install works without reinstalling the
console entry point. Run `python -m pip install -e ./app` from the repository
root for a new install, then restart Codex and check `/mcp`.

The server reuses `tools.build_server()` with a stdio transport. It does not
start Claude, load credentials, or contact a model. The Claude SDK remains a
Python dependency for its tool definitions and server factory. stdout is
reserved for MCP messages: child-process teeing is disabled even if
`KAROSPACE_AGENT_STREAM=1`; the usual sanitized reports are returned as tool
results. This connection cannot protect its host's own chat text or tool
arguments and does not disable a client's other tools.

### Environment

| Env var | Effect |
| --- | --- |
| `KAROSPACE_AGENT_MODEL` | Model alias/id; otherwise Sonnet for Claude or the default from Codex's catalogue. `--model` overrides it. |
| `KAROSPACE_AGENT_PROVIDER` | `claude` (default) or `codex`; overridden by `--provider`. |
| `KAROSPACE_CODEX_BIN` | Override the `codex` executable path. |
| `KAROSPACE_BIN` | Override the `karospace` executable. |
| `KAROSPACE_COMPANION` | Override the companion binary path. |
| `KAROSPACE_AGENT_TIMEOUT` | Per-subprocess timeout, seconds (default 3600). |
| `KAROSPACE_AGENT_STREAM` | `0` silences the default console tee of child-process progress (a front end that installs its own sink is unaffected). |
| `KAROSPACE_AGENT_HISTORY_DIR` | Dedicated local history directory; defaults to `~/.karospace-agent/history`. Use a private, unsynced directory. |

### Local run history

The **History** button in the desktop/browser app shows the latest 100 tool runs
across sessions and providers. Claude, Codex, and standalone MCP use the same
recorder. Each run records its timestamps, status, provider/model selection,
decoded parameters, input/output file metadata, app-environment package versions,
and actual command lines. Command records include executable metadata, a version
when available, and hashes of local workflow script source. Package versions are
explicitly scoped to the app interpreter; a separate scientific interpreter may
have different installed packages. These records aid reproducibility but do not
snapshot datasets or the complete software environment.

Records live only on this computer as owner-readable JSON files in
`~/.karospace-agent/history/<session>/<run>.json` (directories `0700`, files `0600`
on POSIX). They contain local paths and parameters that may identify people, so
keep the directory private. Dataset contents, raw logs, chat messages, and
environment credentials are not recorded. History is never attached to model
requests or exposed as an MCP tool. The history HTTP endpoint accepts only local
app requests and disables browser caching; the UI renders record text without HTML.

A start record is written before a tool runs and command details before each
subprocess starts. Completed, failed, and cancelled calls get a final status.
After a crash, a `started` record means completion was not recorded; it does not
prove the output is valid. Recoverable processing steps also record local SHA-256
fingerprints of their inputs and completed dataset outputs. Computing these
fingerprints streams file bytes locally and may take time for large datasets;
neither the bytes nor the fingerprints are sent to a model.
If history cannot create a start record, the tool returns
`local_history_unavailable` without running. If saving the final status fails,
the tool's result is preserved and the local history view reports a warning.

There is no automatic expiry. To remove history, close the app/MCP server and
delete the relevant session folders in the history directory. This does not
delete datasets or generated outputs. The UI displays up to 100 records; older
records remain on disk until removed locally.

### Dataset readiness

`check_readiness` scans `.h5ad` and AnnData/SpatialData `.zarr` in bounded chunks.
It validates matrix storage and metadata dimensions, finite spatial coordinates,
the selected section column, and finite/nonnegative/integer-valued counts when
requested. Multi-table SpatialData requires an explicit table selection. Its
response contains only fixed diagnostic codes, booleans and aggregate counts;
no identifiers or values are returned.

The registered QC, clustering, UMAP, section splitting/preview, companion and
export tools enforce a fresh check before processing, across Claude, Codex and
stdio MCP. Errors block execution; there is no model-controlled bypass. QC
requires raw counts in X. QC, clustering and UMAP do not require spatial
coordinates. Spatial operations check their selected coordinate and section
keys; export can also use numeric obs columns for coordinates. Companion requires
`prepare` with an explicit `--output`. Run `check_readiness` separately to diagnose
problems early (`require_spatial=false` for nonspatial operations).
Use `coordinate_mode=export` or `companion` to match those readers' coordinate
fallbacks. Export checks `sample_id` when no `--section-key` is supplied; choose
an explicit section column or an empty value for a single section. Multi-table
SpatialData and missing section columns need explicit choices at this boundary.

Memory/disk estimates are advisory guidance, not guarantees for every algorithm; warnings
need review. The tool tests output writability and reports available resources.
It is a workflow check, not an operating-system memory limit. Raw-count validation
does not establish provenance or prove that integer-valued data was never normalized.
It also does not establish consent or make restricted data safe for cloud use.

### Offline chat (macOS Apple Silicon)

For a double-clickable **KaroSpace Offline.app**, run from `app/`:

```bash
python packaging/build_offline_app.py
open "dist/KaroSpace Offline.app"
```

The app offers **Choose dataset…** or **Start chat** and always uses offline
mode. This local bundle refers to the existing checkout, runtime and model;
keep those in place. It can be moved to Applications on this Mac, but is not
a portable installer for another machine. See the
[offline packaging instructions](packaging/README.md#offline-desktop-app).

Use an already-downloaded MLX model and a Python environment with the `offline`
extra installed. Dependency/model acquisition happens before a restricted-data
session. Tk is required for the native window (included with many Python builds).

```bash
pip install -e '.[offline]'
karospace-agent app /path/to/input.h5ad --offline --local-model /path/to/local-mlx-model
```

`chat --offline` provides the terminal equivalent. `--offline-python /path/to/python`
selects a separate runtime. In a source checkout, `.venv-offline/bin/python` is
used if present. If `--local-model` is omitted, the app looks for an existing
`output/offline-models/qwen2.5-0.5b-instruct-4bit` directory in the checkout,
then a cached `mlx-community/Qwen2.5-0.5B-Instruct-4bit` snapshot, then the larger
cached `mlx-community/Qwen3-4B-Instruct-2507-4bit`. It never downloads a model.
The small model uses less CPU; larger models generally reason better but can
take several minutes per turn in this CPU-only mode.
Small models (up to roughly 600 million parameters) are expanded in memory
to use faster CPU matrix operations. Allow about 1–2 GB for their weights,
plus runtime/context memory. Larger models keep their quantized weights.

This launches a native plain-text window with an in-process CPU model. The OS
blocks network access for the app and its child processes, including connections
to localhost and Unix sockets. Section previews are displayed as local PNGs.
The process verifies its restrictions before loading the model or input. Files
created by the session stay in a new private folder under
`~/Library/Application Support/KaroSpaceAgent/offline/`; the terminal and window
show its exact location. Provider credentials/proxy settings are not inherited.
The selected input and model are read-only, and unrelated data files are blocked.
New input access requires restarting with that input selected.

CPU replies can be slow. Network-dependent acquisition/analytics are unavailable,
and viewers are saved rather than opened automatically. **Stop and close** stops
the session and its tools. Existing cloud modes are unchanged. This protects the
app's process tree; it cannot control unrelated backup/sync applications or
establish permission under a donation agreement.

`karospace-agent isolation-check` tests OS-level network denial separately
using synthetic socket operations and a child process. It loads no dataset or
model and returns only fixed diagnostics. Failure is never treated as permission
to run without isolation. This diagnostic alone does **not** switch the app
offline; use `--offline` for a confined session. See the
[offline isolation design](../docs/design/offline-isolation.md) for enforcement
details, platform limits and the remaining work.

### Adding UMAP to an analyzed dataset

`add_umap` writes a new `.h5ad` with a 2D `X_umap` using an existing PCA or latent
representation selected from `inspect_structure` (default `X_pca`). It preserves
expression, layers, annotations, graphs, metadata and existing embeddings. An
existing `X_umap` is kept; an existing `umap` is copied to the standard key. If
there is no suitable representation, choose an analysis with the researcher.
Use `run_preprocess` when clustering is intended, rather than just to add UMAP.
The new step uses the same local history and recovery support as preprocessing.

### Recovering after an interruption

Open **History** after restarting the app. **Continue from checkpoint** verifies
that a completed `.h5ad`/`.zarr` output matches its saved fingerprint and passes a
local structural/readiness check. **Recover this step** verifies the interrupted
step's inputs and prepares its saved parameters with a new output location.
Previous partial outputs are preserved. Approval rechecks file fingerprints in
case something changed while the preview was open.

Recovery creates an aliased continuation for the normal privacy preview. Nothing
is sent until you approve it. The model then continues the workflow, asking for
scientific/display choices that were not recorded; raw chat history is not restored.
Retries use the currently installed software, not a restored environment.

Supported steps include QC, preprocessing, splitting, conversion, ingestion,
merging, export retries, and companion `prepare` with an explicit `--output`.
In-place companion calls, older records without fingerprints, changed/missing
files, and runs whose original process may still be active require manual review.
Completed HTML/package outputs are not offered as dataset checkpoints: validating
HTML alone cannot prove all sidecars are complete. Recovery does not automatically
rerun operations or guess whether an unfinished output is usable.

## Tests

```bash
cd app
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
```

`test_sanitize.py` and `test_commands.py` cover the boundary and command layers —
they run without the SDK or a live model. `test_cli.py` covers the chat REPL
with a scripted stdin and a fake session, and `test_web.py` the event hub, turn
worker, and HTTP routes (both need the SDK importable, no model). `test_auth.py`
covers credential precedence against temp config dirs. (The `PYTEST_DISABLE_PLUGIN_AUTOLOAD`
flag sidesteps an unrelated broken pytest plugin in some conda envs.)

`test_mcp.py` starts a real stdio server and checks tool discovery, inspection
sanitization, artifact metadata, error results, and protocol cleanliness with a
synthetic CLI. It needs neither real datasets nor model credentials.

`test_codex.py` covers the app-server transport with a fake Codex process,
multi-turn conversations, routing and error handling, local sanitization,
worker cancellation, and provider selection in the CLI and web app.

`test_privacy.py` puts synthetic identifiers in paths, labels, logs, unexpected
fields and exceptions and checks model-bound responses. It also verifies that
both backends reject unreviewed messages. `test_privacy_matrix.py` requires a
success fixture for every registered tool and tests success, errors, and
exceptions through the shared Claude/MCP wrapper and Codex RPC response.
`test_history.py` covers persistence, private-file permissions, cancellation,
write failures, command provenance, and local-only access. Tests use temporary
history directories and synthetic identifiers, never the researcher's history.
For the installed Codex runtime,
run the opt-in localhost probe after a CLI upgrade:

```bash
KAROSPACE_TEST_CODEX=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests/test_codex_local_endpoint.py
```

The probe captures a request at a local mock endpoint, with no real dataset or
model credentials, and checks that private instructions and filesystem tools
are absent. It requires permission to bind a local socket.
