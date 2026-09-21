"""The assembled system prompt.

The decision content lives in `playbook.py` as a registry of pipeline-stage
skills; this module just composes them into the strings the agent loop consumes.
`SYSTEM_PROMPT` and `CHAT_ADDENDUM` are kept here as the stable import surface
(agent.py, tests) — swapping the monolith for the stage registry did not change
what a caller imports.
"""

from .playbook import CHAT_ADDENDUM, build_system_prompt

SYSTEM_PROMPT = build_system_prompt()

__all__ = ["SYSTEM_PROMPT", "CHAT_ADDENDUM"]
