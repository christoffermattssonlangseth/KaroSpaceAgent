"""The chat REPL: turn routing, quit handling, and the opening build message.
Uses a scripted stdin and a fake Session, so no SDK process or model runs."""

import asyncio

import pytest

pytest.importorskip("claude_agent_sdk")  # cli imports agent, which imports the SDK

from karospace_agent import cli  # noqa: E402


class FakeSession:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        return f"reply to {text}"


def scripted(lines):
    """A read_line stand-in that yields each line, then EOF (None)."""
    it = iter(lines)

    def read():
        return next(it, None)

    return read


def test_chat_loop_sends_first_message_then_user_turns():
    s = FakeSession()
    asyncio.run(cli.chat_loop(s, "opening build", scripted(["hello", "/quit"])))
    assert s.sent == ["opening build", "hello"]


def test_chat_loop_without_first_message_starts_with_user():
    s = FakeSession()
    asyncio.run(cli.chat_loop(s, None, scripted(["what do you need?"])))
    assert s.sent == ["what do you need?"]


def test_chat_loop_skips_blank_lines_and_stops_on_eof():
    s = FakeSession()
    asyncio.run(cli.chat_loop(s, None, scripted(["", "   ", "one", ""])))
    assert s.sent == ["one"]


@pytest.mark.parametrize("cmd", ["/quit", "/exit", "/q", "/QUIT"])
def test_chat_loop_quit_commands(cmd):
    s = FakeSession()
    asyncio.run(cli.chat_loop(s, None, scripted([cmd, "never sent"])))
    assert s.sent == []


def test_parser_chat_accepts_no_input():
    args = cli.build_parser().parse_args(["chat"])
    assert args.command == "chat"
    assert args.input is None
    assert args.intent == ""


def test_parser_chat_accepts_input_and_intent():
    args = cli.build_parser().parse_args(["chat", "/d/x.h5ad", "grid by sample"])
    assert args.input == "/d/x.h5ad"
    assert args.intent == "grid by sample"


def test_compose_prompt_default_intent():
    text = cli._compose_prompt("/d/x.h5ad", "  ")
    assert "Input file: /d/x.h5ad" in text
    assert "most useful viewer" in text


def test_chat_options_append_addendum_and_keep_boundary():
    from karospace_agent import agent, prompt

    one_shot = agent.build_options()
    chat = agent.build_options(chat=True)
    assert one_shot.system_prompt == prompt.SYSTEM_PROMPT
    assert chat.system_prompt == prompt.SYSTEM_PROMPT + prompt.CHAT_ADDENDUM
    # The boundary must be identical in both modes.
    for opts in (one_shot, chat):
        assert opts.tools == []
        assert opts.allowed_tools == agent.ALLOWED_TOOL_NAMES
        assert opts.setting_sources is None
