"""Codex protocol, local tools, interruption and provider routing, without a model."""
import asyncio
import json
from pathlib import Path
import sys

import pytest

from karospace_agent import agent, cli, codex
from karospace_agent.privacy import Boundary

def approved(session, text):
    draft = session.boundary.preview(text)
    return session.boundary.approve(draft["draft_id"])


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    script = tmp_path / "codex"
    script.write_text('''#!/usr/bin/env python3
import json, os, sys
def emit(message):
    print(json.dumps(message), flush=True)
if sys.argv[1:3] == ['login', 'status']:
    print('Logged in with API key SECRET_SUFFIX')
    sys.exit(0)
turn = 0
for line in sys.stdin:
    m = json.loads(line)
    method, params = m.get('method'), m.get('params', {})
    ident = m.get('id')
    if method == 'initialize':
        emit({'id': ident, 'result': {'userAgent': 'fake_codex'}})
    elif method == 'account/read':
        account = None if os.environ.get('FAKE_CODEX_AUTH') == '0' else {'type': 'chatgpt'}
        emit({'id': ident, 'result': {'requiresOpenaiAuth': True, 'account': account}})
    elif method == 'model/list':
        emit({'id': ident, 'result': {'data': [{'model': 'available-default', 'isDefault': True}]}})
    elif method == 'thread/start':
        assert params['environments'] == []
        assert params['ephemeral'] is True
        assert len(params['dynamicTools']) == 17
        assert params['modelProvider'] == 'openai'
        environments = [{}] if os.environ.get('FAKE_CODEX_ENVIRONMENT') == '1' else []
        emit({'id': ident, 'result': {'thread': {'id': 'thread-1', 'environments': environments}}})
    elif method == 'turn/start':
        assert params['threadId'] == 'thread-1'
        turn += 1
        text = params['input'][0]['text']
        if text == 'disconnect':
            sys.exit(1)
        emit({'id': ident, 'result': {'turn': {'id': str(turn)}}})
        emit({'method': 'turn/started', 'params': {'turn': {'id': str(turn)}}})
        if text == 'hang':
            continue
        if text in ('tool', 'unknown', 'longtool'):
            name = 'not_allowed' if text == 'unknown' else ('run_export' if text == 'longtool' else 'validate_output')
            args = {'input_path':'fake.h5ad', 'output':'fake.html', 'flags':[]} if text == 'longtool' else {'paths':['/karo/output/nonexistent.html']}
            emit({'id': 'tool-1', 'method': 'item/tool/call', 'params': {
                'threadId': 'thread-1', 'turnId': str(turn), 'callId': 'call-1',
                'tool': name, 'arguments': args}})
            continue
        emit({'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'text': 'reply ' + str(turn)}}})
        emit({'method': 'turn/completed', 'params': {'turn': {'id': str(turn), 'status': 'completed'}}})
    elif method == 'turn/interrupt':
        emit({'id': ident, 'result': {}})
        emit({'method': 'turn/completed', 'params': {'turn': {'id': str(turn), 'status': 'interrupted'}}})
    elif ident == 'tool-1':
        result = m['result']
        emit({'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'text': json.dumps(result)}}})
        emit({'method': 'turn/completed', 'params': {'turn': {'id': str(turn), 'status': 'completed'}}})
''')
    script.chmod(0o755)
    monkeypatch.setenv("KAROSPACE_CODEX_BIN", str(script))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-home"))
    return script


def test_codex_session_reuses_thread_and_dispatches_only_known_tools(fake_codex):
    async def exercise():
        events = []
        async with codex.Session(on_event=lambda *event: events.append(event)) as session:
            assert await session.send(approved(session, "hello")) == "reply 1"
            assert await session.send(approved(session, "follow up")) == "reply 2"
            checked = json.loads(await session.send(approved(session, "tool")))
            assert checked["success"]
            assert json.loads(checked["contentItems"][0]["text"])["artifacts"][0]["exists"] is False
            rejected = json.loads(await session.send(approved(session, "unknown")))
            assert not rejected["success"]
            assert "local_tool_error" in rejected["contentItems"][0]["text"]
            assert any(kind == "tool" for kind, _ in events)
            process = session._process
        assert process.returncode is not None
    asyncio.run(asyncio.wait_for(exercise(), 15))


def test_interrupt_allows_next_turn(fake_codex):
    async def exercise():
        async with codex.Session(on_event=lambda *_: None) as session:
            task = asyncio.create_task(session.send(approved(session, "hang")))
            while session._turn is None:
                await asyncio.sleep(.01)
            await session.interrupt()
            assert await task == ""
            assert await session.send(approved(session, "continue")) == "reply 2"
    asyncio.run(asyncio.wait_for(exercise(), 15))


def test_disconnect_surfaces_error_instead_of_hanging(fake_codex):
    async def exercise():
        async with codex.Session(on_event=lambda *_: None) as session:
            with pytest.raises(RuntimeError, match="disconnected"):
                await session.send(approved(session, "disconnect"))
            assert await session.send(approved(session, "hello")) == "reply 1"
    asyncio.run(asyncio.wait_for(exercise(), 15))


def test_codex_auth_does_not_echo_credential_output(fake_codex):
    status = codex.detect()
    assert status.ok
    assert "SECRET" not in status.line()


def test_codex_home_reuses_only_credentials(fake_codex, tmp_path, monkeypatch):
    source = tmp_path / "source-home"
    source.mkdir()
    (source / "auth.json").write_text('{"synthetic": true}')
    (source / "AGENTS.md").write_text("PRIVATE_INSTRUCTIONS")
    (source / "config.toml").write_text('developer_instructions = "PRIVATE_INSTRUCTIONS"')
    monkeypatch.setenv("CODEX_HOME", str(source))
    async def exercise():
        async with codex.Session(on_event=lambda *_: None) as session:
            assert await session.send(approved(session, "hello")) == "reply 1"
            private = Path(session._workspace.name) / "codex-home"
            assert json.loads((private / "auth.json").read_text()) == {"synthetic": True}
            assert (private.stat().st_mode & 0o777) == 0o700
            assert ((private / "auth.json").stat().st_mode & 0o777) == 0o600
            assert not (private / "config.toml").exists()
            assert not (private / "AGENTS.md").exists()
        assert not private.exists()
    asyncio.run(exercise())


def test_missing_login_can_retry_after_signin(fake_codex, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_AUTH", "0")
    async def exercise():
        async with codex.Session(on_event=lambda *_: None) as session:
            with pytest.raises(RuntimeError, match="codex login"):
                await session.send(approved(session, "hello"))
            assert session._process is None
            monkeypatch.setenv("FAKE_CODEX_AUTH", "1")
            assert await session.send(approved(session, "hello")) == "reply 1"
    asyncio.run(asyncio.wait_for(exercise(), 15))


def test_refuses_a_thread_with_execution_access(fake_codex, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_ENVIRONMENT", "1")
    async def exercise():
        async with codex.Session(on_event=lambda *_: None) as session:
            with pytest.raises(RuntimeError, match="execution environments are disabled"):
                await session.send(approved(session, "hello"))
            assert session._process is None
    asyncio.run(asyncio.wait_for(exercise(), 15))


def test_worker_preserves_sanitization_and_local_progress(tmp_path, monkeypatch):
    binary = tmp_path / "karospace"
    binary.write_text('''#!/usr/bin/env python3
import sys
if '--inspect-input' in sys.argv:
    print('sample [categorical; 2 values] examples: PRIVATE_VALUE')
else:
    print('local export progress')
''')
    binary.chmod(0o755)
    monkeypatch.setenv("KAROSPACE_BIN", str(binary))
    async def exercise():
        progress = []
        async with codex.Session(on_progress=lambda *event: progress.append(event)) as session:
            result = await session._run_tool("inspect_input", {
                "input_path": "test.h5ad", "spatialdata_table": "",
            })
            assert "PRIVATE_VALUE" not in json.dumps(result)
            assert "categorical; 2 values" in json.dumps(result)
            assert progress == []
            result = await session._run_tool("run_export", {
                "input_path": "test.h5ad", "output": "out.html", "flags": [],
            })
            assert not result["is_error"]
            assert any("local export progress" in line for _, line in progress)
            invalid = await session._run_tool("validate_output", {"paths": "wrong type"})
            assert invalid["is_error"]
    asyncio.run(asyncio.wait_for(exercise(), 15))


def test_worker_cancellation_stops_child_before_it_writes(tmp_path, monkeypatch):
    marker = tmp_path / "should-not-exist"
    started = tmp_path / "started"
    binary = tmp_path / "slow-karospace"
    binary.write_text(
        '#!/usr/bin/env python3\nimport time\nfrom pathlib import Path\n'
        f'Path({str(started)!r}).touch()\n'
        f'time.sleep(2)\nPath({str(marker)!r}).touch()\n'
    )
    binary.chmod(0o755)
    monkeypatch.setenv("KAROSPACE_BIN", str(binary))
    async def exercise():
        async with codex.Session() as session:
            task = asyncio.create_task(session._run_tool("run_export", {
                "input_path": "x", "output": "out", "flags": [],
            }))
            while not started.exists():
                await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(2.1)
            assert not marker.exists()
    asyncio.run(asyncio.wait_for(exercise(), 10))


@pytest.mark.parametrize("command", ["build", "chat", "web", "app", "auth"])
def test_provider_flag_routes_every_entrypoint(command):
    argv = [command, "--provider", "codex"]
    if command == "build":
        argv.append("example.h5ad")
    args = cli.build_parser().parse_args(argv)
    assert args.provider == "codex"
    if command != "auth":
        assert args.model is None  # never pass the Claude alias to Codex


def test_desktop_cli_forwards_provider(monkeypatch):
    from karospace_agent import desktop
    monkeypatch.setattr(cli, "_preflight", lambda *_: [])
    seen = {}
    monkeypatch.setattr(desktop, "run_app", lambda **kwargs: seen.update(kwargs))
    assert cli.main(["app", "--provider", "codex", "--model", "chosen-model"]) == 0
    assert seen["provider"] == "codex" and seen["model"] == "chosen-model"


def test_create_session_selects_codex_without_constructing_claude(monkeypatch):
    monkeypatch.delenv("KAROSPACE_AGENT_MODEL", raising=False)
    monkeypatch.setattr(agent, "Session", lambda **_: pytest.fail("Claude was instantiated"))
    assert isinstance(agent.create_session("codex"), codex.Session)
    assert agent.create_session("codex").model is None


def test_web_routes_selected_provider_and_auth(monkeypatch):
    from karospace_agent import web
    from starlette.testclient import TestClient
    from karospace_agent.auth import AuthStatus
    seen = []
    class FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
        async def send(self, text): return "reply"
    def factory(provider, model, **kwargs):
        seen.append(provider)
        return FakeSession()
    monkeypatch.setattr(agent, "create_session", factory)
    monkeypatch.setattr(agent, "auth_status", lambda p: AuthStatus(p, p, True))
    with TestClient(web.create_app(provider="codex")) as client:
        assert client.get("/auth").json()["provider"] == "codex"
        assert "Review the outgoing text" in client.get("/").text
    assert seen == ["codex"]
