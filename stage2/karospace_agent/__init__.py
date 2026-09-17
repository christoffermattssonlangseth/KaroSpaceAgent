"""KaroSpaceAgent — Stage 2 (Claude Agent SDK).

Graduates the proven Stage 1 workflow (a Claude Code skill + subagent) into a
hostable Python app built on the Claude Agent SDK. Same architecture, enforced
in code this time:

    local hands   — every side effect runs locally as a subprocess wrapper
    Claude brain  — the model only reasons and chooses flags
    metadata only — the only thing that ever reaches the model is sanitized
                    metadata (column names, dtypes, example values, CLI help,
                    error text). Never the expression matrix, coordinates, or
                    patient identifiers.

The boundary is not a convention here: the agent runs with *all built-in tools
disabled* (`ClaudeAgentOptions(tools=[])`), so the model's only capabilities are
the sanitizing wrappers in `tools.py`. It cannot read a file off disk.
"""

__version__ = "0.1.0"
