"""karospace-agent — the standalone KaroSpaceAgent app.

A hostable Python program on the Claude Agent SDK that drives the `karospace`
CLI with Claude. (The same playbook also ships as Claude Code config in the repo
root; this is the standalone surface.) Architecture, enforced in code:

    local hands  — every side effect runs locally as a subprocess wrapper
    Claude brain — the model only reasons and chooses flags
    schema only  — the only thing that ever reaches the model is the sanitized
                   schema (column names, dtypes, cardinalities, missing/aggregate
                   counts, CLI help, error text). No data values: not the
                   expression matrix, coordinates, patient identifiers, sample
                   IDs, or inspect example values.

The boundary is not a convention here: the agent runs with *all built-in tools
disabled* (`ClaudeAgentOptions(tools=[])`), so the model's only capabilities are
the sanitizing wrappers in `tools.py`. It cannot read a file off disk.
"""

__version__ = "0.1.0"
