# KaroSpaceAgent — Stage 2 (Claude Agent SDK)

The Stage 1 workflow (a Claude Code skill + subagent) proven, graduated into a
hostable Python app on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk).
Same architecture, now enforced in code:

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
cd stage2
pip install -e .          # pulls claude-agent-sdk
```

Requires `karospace` on PATH. The Rust companion is optional; the app finds it at
`../../KaroSpaceCompanion/target/release/karospace-companion` or via
`KAROSPACE_COMPANION`.

## Use

```bash
karospace-agent build ~/data/my_xenium.h5ad \
  "grid by sample, colour by cell_type, focus on Cd4/Cd8a/Gfap"
```

The agent inspects the file, chooses flags, (optionally) runs the companion, runs
the export, reads errors and iterates, produces both the sidecar viewer and the
`.karospace` package, and validates the output — streaming progress as it goes.

## Layout

| File | Role |
| --- | --- |
| `commands.py` | Subprocess wrappers; locates `karospace` / companion / merge script. No model contact. |
| `sanitize.py` | The boundary: output truncation + path *stat* (never file bytes). |
| `tools.py` | The seven `@tool` local hands, each returning sanitized text. |
| `prompt.py` | System prompt — the Stage 1 playbook, ported to the tools. |
| `agent.py` | Builds `ClaudeAgentOptions` (built-ins off) and runs the query loop. |
| `cli.py` | `karospace-agent build <input> "<intent>"`. |

## Config

| Env var | Effect |
| --- | --- |
| `KAROSPACE_AGENT_MODEL` | Model alias/id (default `sonnet`). |
| `KAROSPACE_BIN` | Override the `karospace` executable. |
| `KAROSPACE_COMPANION` | Override the companion binary path. |
| `KAROSPACE_AGENT_TIMEOUT` | Per-subprocess timeout, seconds (default 3600). |

## Tests

```bash
cd stage2
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
```

`test_sanitize.py` and `test_commands.py` cover the boundary and command layers —
they run without the SDK or a live model. (The `PYTEST_DISABLE_PLUGIN_AUTOLOAD`
flag sidesteps an unrelated broken pytest plugin in some conda envs.)
