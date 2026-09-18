# `karospace-agent` — the standalone app

The `karospace-agent` app: a hostable Python program on the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk) that drives the
`karospace` CLI with Claude. (The same playbook also ships as Claude Code config
in the repo root — this is the standalone surface.) Architecture, enforced in
code:

- **local hands** — every side effect is a subprocess wrapper (`commands.py`);
- **Claude brain** — the model only reasons and chooses flags (`prompt.py`);
- **schema only** — the model's *entire* capability surface is the seven
  sanitizing tools in `tools.py`, and **all built-in tools are disabled**
  (`ClaudeAgentOptions(tools=[])`). The model literally cannot read a file off
  disk — it sees column names, dtypes, cardinalities, missing/aggregate counts,
  CLI help, and error text, but **no data values**: `inspect_input` strips the
  per-column example values (`strip_inspect_examples`), so the expression matrix,
  coordinates, patient identifiers, and sample IDs never cross the boundary.

## Install

```bash
cd app
pip install -e .          # pulls claude-agent-sdk
```

Requires `karospace` on PATH. The Rust companion is optional; the app finds it at
`../../KaroSpaceCompanion/target/release/karospace-companion` or via
`KAROSPACE_COMPANION`.

Then `karospace-agent auth` — it reports which Claude credential the model will
run under (a Console sign-in via `claude` `/login`, an API key, federation, or a
cloud provider) and prints sign-in instructions if none is found. A claude.ai
subscription login is flagged as not permitted for this app; see the root README
→ *Authentication* for why. `auth.py` mirrors Claude Code's credential
precedence from the outside, reading env-var presence and file key names only,
never a secret value.

## Use

```bash
karospace-agent build ~/data/my_xenium.h5ad \
  "grid by sample, colour by cell_type, focus on Cd4/Cd8a/Gfap"
```

The agent inspects the file, chooses flags, (optionally) runs the companion, runs
the export, reads errors and iterates, produces both the sidecar viewer and the
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
built-ins off, the seven tools, and a short conversation addendum to the system
prompt. Anything you type goes to the model verbatim, so give paths and column
names, not values.

### Browser mode

```bash
pip install -e '.[web]'          # starlette + uvicorn
karospace-agent web              # or: karospace-agent web <input> "<intent>"
```

Opens one conversation at `http://127.0.0.1:8765/` (`--host`, `--port`). The
server owns a single `Session` and runs user turns one at a time; the page
follows a Server-Sent Events stream (`GET /events`, replayed on reconnect),
posts turns to `/send`, and `/interrupt` stops the running turn. Child-process
progress arrives on the same stream via the progress sink. Localhost only by
default and no auth: it is a local app with a browser window, not a service.

## Layout

| File | Role |
| --- | --- |
| `commands.py` | Subprocess wrappers; locates `karospace` / companion / merge script. No model contact. |
| `sanitize.py` | The boundary: output truncation + path *stat* (never file bytes). |
| `tools.py` | The seven `@tool` local hands, each returning sanitized text. |
| `prompt.py` | System prompt — the viewer-building playbook, ported to the tools. |
| `agent.py` | Builds `ClaudeAgentOptions` (built-ins off); `run()` for one-shot builds, `Session` for multi-turn chat. |
| `auth.py` | Detects which credential the CLI subprocess will use and whether it is permitted; no secret is read. |
| `cli.py` | `karospace-agent build <input> "<intent>"`, `karospace-agent chat [input] ["<intent>"]` (the REPL), `karospace-agent web`, and `karospace-agent auth`. |
| `web.py` | The browser front end: Starlette app, SSE event hub, one-turn-at-a-time worker (optional `[web]` extra). |
| `static/index.html` | The single-file page `web.py` serves. |

## Config

| Env var | Effect |
| --- | --- |
| `KAROSPACE_AGENT_MODEL` | Model alias/id (default `sonnet`). |
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
