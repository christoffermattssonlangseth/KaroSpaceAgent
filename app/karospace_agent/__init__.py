"""karospace-agent — the standalone KaroSpaceAgent app.

A Python program that drives the local `karospace` CLI with Claude or Codex.
The same playbook also ships as repository skills. Architecture:

    local hands  — every side effect runs locally as a subprocess wrapper
    model brain  — the model reasons and chooses flags
    schema only  — the only thing that ever reaches the model is the sanitized
                   schema (column names, dtypes, cardinalities, missing/aggregate
                   counts, CLI help, error text). No data values: not the
                   expression matrix, coordinates, patient identifiers, sample
                   IDs, or inspect example values.

Claude disables built-in tools with `ClaudeAgentOptions(tools=[])`. Codex has
no attached execution environment and dispatches the sanitizing wrappers in
`tools.py` through local workers.
"""

__version__ = "0.1.0"
