"""The agent loop: wire the sanitizing tools to Claude and stream the run.

The boundary is enforced here by two options:

    tools=[]            -> disable every built-in tool. The model's only
                           capabilities are our MCP tools; it cannot Read a file
                           or run Bash. Sanitized metadata is the only channel.
    setting_sources=None -> ignore the host's CLAUDE.md / settings, so the app
                           behaves identically wherever it runs.
"""

from __future__ import annotations

import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    query,
)

from .commands import REPO_ROOT
from .prompt import SYSTEM_PROMPT
from .tools import ALLOWED_TOOL_NAMES, build_server

# Alias, not a pinned id, so the app tracks the current Sonnet. Override with
# KAROSPACE_AGENT_MODEL (e.g. a full model id) if you need to.
DEFAULT_MODEL = os.environ.get("KAROSPACE_AGENT_MODEL", "sonnet")


def build_options(model: str = DEFAULT_MODEL) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=model,
        mcp_servers={"karospace": build_server()},
        allowed_tools=ALLOWED_TOOL_NAMES,  # auto-allow exactly our tools
        tools=[],                          # disable ALL built-in tools
        permission_mode="bypassPermissions",  # headless; nothing else can run anyway
        setting_sources=None,              # ignore host CLAUDE.md / settings
        cwd=str(REPO_ROOT),
    )


async def run(user_prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Drive one build to completion, streaming progress to stdout.

    Returns the final result text (empty string if none)."""
    options = build_options(model)
    final = ""

    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, flush=True)
                elif isinstance(block, ToolUseBlock):
                    print(f"  · {block.name}({_brief(block.input)})", flush=True)
                elif isinstance(block, ToolResultBlock):
                    pass  # results are large + already sanitized; don't echo
        elif isinstance(message, ResultMessage):
            final = getattr(message, "result", "") or ""

    return final


def _brief(tool_input: object, limit: int = 80) -> str:
    text = str(tool_input)
    return text if len(text) <= limit else text[:limit] + "…"
