"""The agent loop: wire the sanitizing tools to Claude and stream the run.

The boundary is enforced here by two options:

    tools=[]            -> disable every built-in tool. The model's only
                           capabilities are our MCP tools; it cannot Read a file
                           or run Bash. Sanitized metadata is the only channel.
    setting_sources=None -> ignore the host's CLAUDE.md / settings, so the app
                           behaves identically wherever it runs.

Two entry points share one option set and one renderer:

    run(prompt, review=...) -> one-shot build with an async local review callback.
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
)

from .commands import REPO_ROOT, ProgressSink, set_progress_sink
from .prompt import CHAT_ADDENDUM, SYSTEM_PROMPT
from .tools import ALLOWED_TOOL_NAMES, build_server
from .privacy import Boundary, PRIVACY_INSTRUCTIONS

# Alias, not a pinned id, so the app tracks the current Sonnet. Override with
# KAROSPACE_AGENT_MODEL (e.g. a full model id) if you need to.
DEFAULT_MODEL = os.environ.get("KAROSPACE_AGENT_MODEL", "sonnet")
DEFAULT_PROVIDER = os.environ.get("KAROSPACE_AGENT_PROVIDER", "claude")


def create_session(provider: str = DEFAULT_PROVIDER, model: str | None = None, **kwargs):
    model = model or os.environ.get("KAROSPACE_AGENT_MODEL")
    if provider == "codex":
        from .codex import Session as CodexSession
        return CodexSession(model=model, **kwargs)
    if provider != "claude":
        raise ValueError(f"Unknown provider: {provider}")
    return Session(model=model or "sonnet", **kwargs)


def auth_status(provider: str = DEFAULT_PROVIDER):
    if provider == "codex":
        from .codex import detect
    elif provider == "claude":
        from .auth import detect
    else:
        raise ValueError(f"Unknown provider: {provider}")
    return detect()


def build_options(model: str = DEFAULT_MODEL, chat: bool = False, boundary=None, cwd=None) -> ClaudeAgentOptions:
    system_prompt = SYSTEM_PROMPT + CHAT_ADDENDUM if chat else SYSTEM_PROMPT
    return ClaudeAgentOptions(
        system_prompt=system_prompt + PRIVACY_INSTRUCTIONS,
        model=model,
        mcp_servers={"karospace": build_server(boundary)},
        allowed_tools=ALLOWED_TOOL_NAMES,  # auto-allow exactly our tools
        tools=[],                          # disable ALL built-in tools
        permission_mode="bypassPermissions",  # headless; nothing else can run anyway
        setting_sources=None,              # ignore host CLAUDE.md / settings
        cwd=cwd or str(REPO_ROOT),
    )


# What a front end receives from a turn, as (kind, text):
#   "text"  — a block of the model's reply
#   "tool"  — one tool call, rendered as `name(args…)`
#   "result"— the turn's final text (the ResultMessage)
EventSink = Callable[[str, str], None]


def console_events(kind: str, text: str) -> None:
    """Default sink: the model's text and tool calls to stdout."""
    if kind == "text":
        print(text, flush=True)
    elif kind == "tool":
        print(f"  · {text}", flush=True)


def _emit(message: object, on_event: EventSink = console_events) -> str | None:
    """Fan one SDK message out to `on_event`. Returns the final result text when
    the message is the turn's ResultMessage, else None."""
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                on_event("text", block.text)
            elif isinstance(block, ToolUseBlock):
                on_event("tool", f"{block.name}({_brief(block.input)})")
            elif isinstance(block, ToolResultBlock):
                pass  # results are large + already sanitized; don't echo
    elif isinstance(message, ResultMessage):
        result = getattr(message, "result", "") or ""
        on_event("result", result)
        return result
    return None


async def run(user_prompt: str, model: str | None = None, provider: str = DEFAULT_PROVIDER, review=None) -> str:
    """Drive one build to completion, streaming progress to stdout.

    Returns the final result text (empty string if none)."""
    if review is None:
        raise ValueError("An async local privacy-review callback is required for agent.run().")
    async with create_session(provider, model) as session:
        preview = session.boundary.preview(user_prompt)
        if not await review(preview["text"]):
            return ""
        return await session.send(session.boundary.approve(preview["draft_id"]))


class Session:
    """One multi-turn conversation with the agent.

    Usage::

        async with Session() as s:
            draft = s.boundary.preview("Input file: /data/x.h5ad")
            # Show draft["text"] locally and obtain explicit human approval.
            await s.send(s.boundary.approve(draft["draft_id"]))

    Each `send` is one turn: the model may call tools any number of times, then
    replies. Context (what it inspected, what it chose, what it asked) carries
    over to the next turn. The option set is identical to the one-shot build,
    so the data boundary is unchanged: built-in tools are off and the only
    capabilities are the sanitizing wrappers in `tools.py`.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        on_event: EventSink = console_events,
        on_progress: ProgressSink | None = None,
    ) -> None:
        import tempfile
        self.boundary = Boundary(provider="claude", model=model)
        self._workspace = tempfile.TemporaryDirectory(prefix="karospace-claude-")
        self._client = ClaudeSDKClient(options=build_options(
            model, chat=True, boundary=self.boundary, cwd=self._workspace.name))
        self._on_event = on_event
        self._on_progress = on_progress

    async def __aenter__(self) -> "Session":
        # `on_event` gets the model's text and tool calls; `on_progress` gets the
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
            self._workspace.cleanup()

    async def send(self, text: str) -> str:
        """Send one user turn and stream the reply. Returns the final text."""
        text = self.boundary.consume(text)
        await self._client.query(text)
        final = ""
        async for message in self._client.receive_response():
            result = _emit(message, lambda kind, value: self._on_event(kind, self.boundary.display(value)))
            if result is not None:
                final = result
        return final

    async def interrupt(self) -> None:
        """Ask the model to stop the current turn."""
        await self._client.interrupt()


def _brief(tool_input: object, limit: int = 80) -> str:
    text = str(tool_input)
    return text if len(text) <= limit else text[:limit] + "…"
