"""The agent loop: wire the sanitizing tools to Claude and stream the run.

The boundary is enforced here by two options:

    tools=[]            -> disable every built-in tool. The model's only
                           capabilities are our MCP tools; it cannot Read a file
                           or run Bash. Sanitized metadata is the only channel.
    setting_sources=None -> ignore the host's CLAUDE.md / settings, so the app
                           behaves identically wherever it runs.

Two entry points share one option set and one renderer:

    run(prompt)   -> one-shot build (`karospace-agent build`), via `query()`.
    Session       -> multi-turn conversation (`karospace-agent chat`), via
                     `ClaudeSDKClient`. Same tools, same prompt, same boundary;
                     the model just keeps context between turns so it can ask
                     the user a question and act on the answer.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    query,
)

from .commands import REPO_ROOT, ProgressSink, set_progress_sink
from .prompt import CHAT_ADDENDUM, SYSTEM_PROMPT
from .tools import ALLOWED_TOOL_NAMES, build_server

# Alias, not a pinned id, so the app tracks the current Sonnet. Override with
# KAROSPACE_AGENT_MODEL (e.g. a full model id) if you need to.
DEFAULT_MODEL = os.environ.get("KAROSPACE_AGENT_MODEL", "sonnet")


def build_options(model: str = DEFAULT_MODEL, chat: bool = False) -> ClaudeAgentOptions:
    system_prompt = SYSTEM_PROMPT + CHAT_ADDENDUM if chat else SYSTEM_PROMPT
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        mcp_servers={"karospace": build_server()},
        allowed_tools=ALLOWED_TOOL_NAMES,  # auto-allow exactly our tools
        tools=[],                          # disable ALL built-in tools
        permission_mode="bypassPermissions",  # headless; nothing else can run anyway
        setting_sources=None,              # ignore host CLAUDE.md / settings
        cwd=str(REPO_ROOT),
    )


def _print(text: str) -> None:
    print(text, flush=True)


def _emit(message: object, out: Callable[[str], None] = _print) -> str | None:
    """Render one SDK message to `out`. Returns the final result text when the
    message is the turn's ResultMessage, else None."""
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                out(block.text)
            elif isinstance(block, ToolUseBlock):
                out(f"  · {block.name}({_brief(block.input)})")
            elif isinstance(block, ToolResultBlock):
                pass  # results are large + already sanitized; don't echo
    elif isinstance(message, ResultMessage):
        return getattr(message, "result", "") or ""
    return None


async def run(user_prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Drive one build to completion, streaming progress to stdout.

    Returns the final result text (empty string if none)."""
    options = build_options(model)
    final = ""

    async for message in query(prompt=user_prompt, options=options):
        result = _emit(message)
        if result is not None:
            final = result

    return final


class Session:
    """One multi-turn conversation with the agent.

    Usage::

        async with Session() as s:
            await s.send("Build a viewer. Input file: /data/x.h5ad ...")
            await s.send("Yes, merge those two sections and rebuild.")

    Each `send` is one turn: the model may call tools any number of times, then
    replies. Context (what it inspected, what it chose, what it asked) carries
    over to the next turn. The option set is identical to the one-shot build,
    so the data boundary is unchanged: built-in tools are off and the only
    capabilities are the sanitizing wrappers in `tools.py`.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        out: Callable[[str], None] = _print,
        on_progress: ProgressSink | None = None,
    ) -> None:
        self._client = ClaudeSDKClient(options=build_options(model, chat=True))
        self._out = out
        self._on_progress = on_progress

    async def __aenter__(self) -> "Session":
        # `out` gets the model's text and tool calls; `on_progress` gets the
        # child processes' live log lines (installed process-wide for the life
        # of the session, restored on exit). Both are local-only channels.
        if self._on_progress is not None:
            set_progress_sink(self._on_progress)
        await self._client.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            await self._client.disconnect()
        finally:
            if self._on_progress is not None:
                set_progress_sink(None)

    async def send(self, text: str) -> str:
        """Send one user turn and stream the reply. Returns the final text."""
        await self._client.query(text)
        final = ""
        async for message in self._client.receive_response():
            result = _emit(message, self._out)
            if result is not None:
                final = result
        return final

    async def interrupt(self) -> None:
        """Ask the model to stop the current turn."""
        await self._client.interrupt()


def _brief(tool_input: object, limit: int = 80) -> str:
    text = str(tool_input)
    return text if len(text) <= limit else text[:limit] + "…"
