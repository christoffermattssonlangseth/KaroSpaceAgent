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
| `sanitize.py` | Local output formatting and path *stat* (never file bytes). |
| `tools.py` | The `@tool` local hands (inspect / acquire / .rds convert / prepare / enrich / export / deliver), wrapped by the privacy boundary before any model reply. |
| `prompt.py` | System prompt — the viewer-building playbook, ported to the tools. |
| `agent.py` | Provider selection and Claude options/session; one-shot builds. |
| `codex.py` | Codex app-server connection, conversation state, tool dispatch and interruption. |
| `tool_worker.py` | Validates and executes one existing tool in a cancellable local worker; the parent filters reports before they reach Codex. |
| `mcp.py` | Serves the same 20 sanitizing tools over stdio for Codex and other MCP clients; no model session or Claude authentication. |
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
both backends reject unreviewed messages. For the installed Codex runtime,
run the opt-in localhost probe after a CLI upgrade:

```bash
KAROSPACE_TEST_CODEX=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests/test_codex_local_endpoint.py
```

The probe captures a request at a local mock endpoint, with no real dataset or
model credentials, and checks that private instructions and filesystem tools
are absent. It requires permission to bind a local socket.
