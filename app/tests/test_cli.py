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


def test_inspect_structure_is_an_allowed_tool():
    from karospace_agent import agent, tools

    assert "inspect_structure" in tools.TOOL_NAMES
    assert "mcp__karospace__inspect_structure" in agent.ALLOWED_TOOL_NAMES


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


class GatedSession(FakeSession):
    """`send` blocks until `interrupt` is called, like a real long turn."""

    def __init__(self):
        super().__init__()
        self.interrupted = 0
        self.gate = asyncio.Event()

    async def send(self, text):
        self.sent.append(text)
        await self.gate.wait()
        return "stopped"

    async def interrupt(self):
        self.interrupted += 1
        self.gate.set()


def test_first_sigint_interrupts_turn_and_returns_to_prompt():
    async def main():
        s = GatedSession()
        holder = {}

        async def turn():
            async with cli.TurnInterrupter(s) as ti:
                holder["ti"] = ti
                return await s.send("long export")

        t = asyncio.create_task(turn())
        await asyncio.sleep(0)  # let the turn start and block on the gate
        holder["ti"].on_sigint()
        assert await t == "stopped"
        assert s.interrupted == 1

    asyncio.run(main())


def test_second_sigint_cancels_turn():
    async def main():
        s = GatedSession()
        s.interrupt = _never_finishes  # the model ignores the first interrupt
        holder = {}

        async def turn():
            async with cli.TurnInterrupter(s) as ti:
                holder["ti"] = ti
                return await s.send("stuck")

        t = asyncio.create_task(turn())
        await asyncio.sleep(0)
        holder["ti"].on_sigint()
        await asyncio.sleep(0)
        holder["ti"].on_sigint()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert t.cancelled()

    asyncio.run(main())


async def _never_finishes():
    await asyncio.sleep(3600)


def test_sigint_handler_is_removed_after_turn():
    import signal

    async def main():
        loop = asyncio.get_running_loop()
        s = FakeSession()
        async with cli.TurnInterrupter(s):
            pass
        # Back to the default: add_signal_handler would now be a fresh install,
        # and removing a handler that is not installed returns False.
        assert loop.remove_signal_handler(signal.SIGINT) is False

    asyncio.run(main())


def test_ainput_reads_on_a_daemon_thread_and_returns_eof_as_none():
    import threading

    names = []

    def read():
        t = threading.current_thread()
        names.append((t.name, t.daemon))
        return None

    assert asyncio.run(cli._ainput(read)) is None
    assert names == [("karospace-agent-stdin", True)]


def test_auth_report_variants():
    from karospace_agent import auth

    text, code = cli.auth_report(auth.AuthStatus("api_key", "API key (ANTHROPIC_API_KEY)", True))
    assert code == 0 and "permitted" in text
    text, code = cli.auth_report(auth.AuthStatus(None, "none", False, "no credential found"))
    assert code == 1 and "Console account" in text
    text, code = cli.auth_report(
        auth.AuthStatus("claudeai_login", "claude.ai login (max)", False, "not permitted")
    )
    assert code == 3 and "NOT PERMITTED" in text and "Console account" in text


def test_preflight_warns_without_credential(monkeypatch):
    from karospace_agent import auth

    monkeypatch.setattr(auth, "detect", lambda: auth.AuthStatus(None, "none", False, "no credential found"))
    notes = cli._preflight()
    assert any("no usable Claude credential" in n for n in notes)
    monkeypatch.setattr(auth, "detect", lambda: auth.AuthStatus("api_key", "API key", True))
    assert not any("credential" in n for n in cli._preflight())


def test_preflight_notes_companion_version_when_present(monkeypatch):
    from karospace_agent import auth

    monkeypatch.setattr(auth, "detect", lambda: auth.AuthStatus("api_key", "API key", True))
    monkeypatch.setattr(cli, "companion_bin", lambda: "/fake/karospace-companion")
    monkeypatch.setattr(cli, "companion_version", lambda: "0.1.0")
    notes = cli._preflight()
    assert any("karospace-companion 0.1.0" in n and "default" in n for n in notes)


def test_preflight_warns_when_companion_missing(monkeypatch):
    from karospace_agent import auth

    monkeypatch.setattr(auth, "detect", lambda: auth.AuthStatus("api_key", "API key", True))
    monkeypatch.setattr(cli, "companion_bin", lambda: None)
    notes = cli._preflight()
    assert any("karospace-companion not found" in n for n in notes)


def test_auth_subcommand_exit_code(monkeypatch, capsys):
    from karospace_agent import auth

    monkeypatch.setattr(auth, "detect", lambda: auth.AuthStatus("console_profile", "Console sign-in", True))
    assert cli.main(["auth"]) == 0
    assert "Console sign-in" in capsys.readouterr().out
