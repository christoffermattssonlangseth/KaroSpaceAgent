"""Codex app-server backend for the standalone chat UI.

No execution environment is attached to the model. KaroSpace's dynamic tools
are dispatched locally through the same allowlisted, sanitizing handlers as
Claude. User MCP servers, plugins and filesystem execution features are off.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from .agent import console_events, _brief
from .auth import AuthStatus
from .commands import REPO_ROOT
from .prompt import SYSTEM_PROMPT, CHAT_ADDENDUM
from .tool_worker import tool_specs
from .privacy import Boundary, PRIVACY_INSTRUCTIONS, result as private_result

SIGNIN_HELP = "Install Codex CLI (`npm install -g @openai/codex`), then run `codex login`."


def executable() -> str | None:
    return os.environ.get("KAROSPACE_CODEX_BIN") or shutil.which("codex")


def detect() -> AuthStatus:
    binary = executable()
    if not binary:
        return AuthStatus(None, "Codex not installed", False, SIGNIN_HELP)
    try:
        result = subprocess.run([binary, "login", "status"], capture_output=True,
                                text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return AuthStatus(None, "Codex login unavailable", False, SIGNIN_HELP)
    if result.returncode:
        return AuthStatus(None, "Codex not signed in", False, SIGNIN_HELP)
    # Never echo CLI credential output (an API-key login can show a key suffix).
    return AuthStatus("codex_login", "Codex · signed in", True)


def isolation_config() -> dict:
    return {
        "model_provider": "openai", "mcp_servers": {}, "plugins": {},
        "web_search": "disabled", "project_doc_max_bytes": 0,
        "developer_instructions": "", "tools.view_image": False,
        "features": {name: False for name in (
            "shell_tool", "unified_exec", "code_mode", "apps",
            "plugins", "multi_agent", "multi_agent_v2", "memories", "hooks",
            "browser_use", "browser_use_external", "computer_use", "image_generation",
            "view_image", "skill_search", "tool_suggest", "shell_snapshot",
        # Newer Codex models dispatch dynamic tools through this JS isolate.
        # It receives no filesystem execution environment; disabling it breaks
        # tool calls even when the code_mode feature flag itself is false.
        )} | {"skip_host_skill_discovery": True, "code_mode_host": True},
    }


def _toml(value) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(k) + " = " + _toml(v) for k, v in value.items()) + "}"
    return json.dumps(value)


async def _stop_process(proc) -> None:
    if proc is None:
        return
    # Workers and their CLI children share a new process group on POSIX.
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
    elif proc.returncode is None:
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 2)
    except asyncio.TimeoutError:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        await proc.wait()
    finally:
        if os.name == "posix":
            # A child may ignore TERM after its worker exits.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)


class Session:
    def __init__(self, model=None, on_event=console_events, on_progress=None):
        self.model = model
        self._on_event = on_event
        self._on_progress = on_progress
        self._process = None
        self._reader = None
        self._pending = {}
        self._sequence = 0
        self._thread = None
        self._turn = None
        self._finished = None
        self._tools = set()
        self._workspace = None
        self._final = []
        self._interrupted = False
        self._lock = asyncio.Lock()
        self._tool_lock = asyncio.Lock()
        self.boundary = Boundary(provider="codex", model=model)

    async def __aenter__(self):
        # Connect lazily: the native window must open even if sign-in is missing.
        return self

    async def __aexit__(self, *exc):
        for task in list(self._tools):
            task.cancel()
        await asyncio.gather(*self._tools, return_exceptions=True)
        await _stop_process(self._process)
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        self._process = self._reader = self._thread = None
        if self._workspace:
            self._workspace.cleanup()
            self._workspace = None

    async def _write(self, message):
        self._process.stdin.write((json.dumps(message) + "\n").encode())
        await self._process.stdin.drain()

    async def _request(self, method, params, timeout=30):
        self._sequence += 1
        ident = self._sequence
        future = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        try:
            await self._write({"id": ident, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(ident, None)

    async def _connect(self):
        binary = executable()
        if not binary:
            raise RuntimeError(SIGNIN_HELP)
        self._workspace = tempfile.TemporaryDirectory(prefix="karospace-codex-")
        cwd = self._workspace.name
        instructions = Path(cwd) / "instructions.md"
        instructions.write_text(SYSTEM_PROMPT + CHAT_ADDENDUM + PRIVACY_INSTRUCTIONS)
        config = isolation_config()
        config["model_instructions_file"] = str(instructions)
        # Reuse authentication only, never the user's instructions, skills,
        # plugins, MCP configuration, or conversation history. Copy locally;
        # this file is outside every model execution environment.
        original_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        private_home = Path(cwd) / "codex-home"
        private_home.mkdir(mode=0o700)
        credential = original_home / "auth.json"
        if credential.is_file():
            target = private_home / "auth.json"
            target.touch(mode=0o600)
            shutil.copyfile(credential, target)
        config["cli_auth_credentials_store"] = "file"
        env = dict(os.environ)
        env["CODEX_HOME"] = str(private_home)
        args = [binary, "app-server"]
        for key, value in config.items():
            args += ["-c", key + "=" + _toml(value)]
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                limit=4 * 1024 * 1024, start_new_session=(os.name == "posix"),
            )
            self._reader = asyncio.create_task(self._read())
            await self._request("initialize", {
                "clientInfo": {"name": "karospace_agent", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            })
            await self._write({"method": "initialized"})
            account = await self._request("account/read", {"refreshToken": False})
            if account.get("requiresOpenaiAuth") and not account.get("account"):
                raise RuntimeError("Codex is not signed in. Run `codex login`, then send your message again.")
            params = {
                "cwd": cwd, "environments": [], "selectedCapabilityRoots": [],
                "ephemeral": True, "approvalPolicy": "never",
                "baseInstructions": SYSTEM_PROMPT + CHAT_ADDENDUM + PRIVACY_INSTRUCTIONS,
                "developerInstructions": "Use only the provided KaroSpace tools. Ask questions in your reply.",
                "dynamicTools": tool_specs(), "modelProvider": "openai",
            }
            if self.model:
                params["model"] = self.model
            else:
                catalog = await self._request("model/list", {"includeHidden": False})
                available = catalog.get("data", [])
                default = next((m for m in available if m.get("isDefault")), None)
                if default is None:
                    raise RuntimeError("Codex returned no default model. Choose one with --model.")
                params["model"] = default["model"]
            self.boundary.history.model = params["model"]
            response = await self._request("thread/start", params)
            if response["thread"].get("environments") != []:
                raise RuntimeError(
                    "Codex did not confirm that execution environments are disabled. "
                    "Update Codex CLI before using this backend."
                )
            self._thread = response["thread"]["id"]
        except BaseException:
            await self.__aexit__()
            raise

    async def _read(self):
        error = RuntimeError("Codex disconnected. Check `codex login` and restart the chat app.")
        try:
            while line := await self._process.stdout.readline():
                message = json.loads(line)
                ident = message.get("id")
                if "method" not in message:
                    future = self._pending.get(ident)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(RuntimeError(message["error"].get("message", "Codex request failed")))
                        else:
                            future.set_result(message.get("result", {}))
                    continue
                method, params = message["method"], message.get("params", {})
                if ident is not None:
                    if method == "item/tool/call":
                        task = asyncio.create_task(self._tool_call(ident, params))
                        self._tools.add(task)
                        task.add_done_callback(self._tools.discard)
                    else:
                        # No host execution, additional permissions, or external tools.
                        await self._write({"id": ident, "error": {
                            "code": -32601, "message": "Use KaroSpace tools or ask the user in your reply.",
                        }})
                elif method == "turn/started":
                    self._turn = params["turn"]["id"]
                    if self._interrupted:
                        asyncio.create_task(self.interrupt())
                elif method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        text = item.get("text", "")
                        self._final.append(text)
                        self._on_event("text", self.boundary.display(text))
                elif method == "turn/completed" and self._finished and not self._finished.done():
                    turn = params["turn"]
                    if turn.get("status") == "failed":
                        self._finished.set_exception(RuntimeError((turn.get("error") or {}).get("message", "Codex turn failed")))
                    else:
                        self._finished.set_result("\n\n".join(self._final))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = RuntimeError(f"Codex connection failed: {exc}")
        finally:
            for future in [*self._pending.values(), self._finished]:
                if future and not future.done():
                    future.set_exception(error)

    async def _tool_call(self, ident, params):
        result = {"is_error": True, "content": [{"type": "text", "text": "Tool interrupted."}]}
        try:
            name = params.get("tool")
            if (params.get("threadId") != self._thread or self._interrupted
                    or params.get("turnId") != self._turn):
                raise ValueError("Tool call does not belong to the active turn.")
            if params.get("namespace") not in (None, "") or name not in {s["name"] for s in tool_specs()}:
                raise ValueError("Unknown KaroSpace tool.")
            self._on_event("tool", f"{name}({_brief(params.get('arguments'))})")
            async with self._tool_lock:
                remote_args = params["arguments"]
                result = await self.boundary.execute(
                    name, remote_args, lambda local_args: self._run_tool(name, local_args))
        except asyncio.CancelledError:
            pass
        except Exception:
            result = private_result({"status": "error", "diagnostic": "local_tool_error"}, True)
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await self._write({"id": ident, "result": {
                "success": not result.get("is_error", False),
                "contentItems": [{"type": "inputText", "text": block["text"]}
                                 for block in result.get("content", []) if block.get("type") == "text"],
            }})

    async def _run_tool(self, name, arguments):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "karospace_agent.tool_worker", cwd=str(REPO_ROOT), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024,
            start_new_session=(os.name == "posix"),
        )
        try:
            proc.stdin.write((json.dumps({"tool": name, "arguments": arguments}) + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()
            result = None
            while line := await proc.stdout.readline():
                event = json.loads(line)
                if event["kind"] == "progress" and self._on_progress:
                    self._on_progress(event["stream"], event["line"])
                elif event["kind"] == "command":
                    from .history import accept_command
                    accept_command(event["command"])
                elif event["kind"] == "result":
                    result = event["result"]
            await proc.wait()
            if result is None:
                raise RuntimeError(f"Local tool worker exited without a result ({proc.returncode}).")
            return result
        finally:
            await _stop_process(proc)

    async def send(self, text):
        text = self.boundary.consume(text)
        async with self._lock:
            self._interrupted = False
            self._turn = None
            if not self._thread:
                await self._connect()
            if self._interrupted:
                return ""
            self._final = []
            self._finished = asyncio.get_running_loop().create_future()
            try:
                response = await self._request("turn/start", {
                    "threadId": self._thread, "input": [{"type": "text", "text": text}],
                })
                self._turn = response["turn"]["id"]
                if self._interrupted:
                    await self.interrupt()
                final = await self._finished
                self._on_event("result", final)
                return final
            except BaseException:
                for task in list(self._tools):
                    task.cancel()
                await asyncio.gather(*self._tools, return_exceptions=True)
                if self._reader and self._reader.done():
                    await self.__aexit__()
                raise
            finally:
                self._turn = None
                if self._finished and not self._finished.done():
                    self._finished.cancel()
                elif self._finished and not self._finished.cancelled():
                    self._finished.exception()  # consume a concurrent disconnect error

    async def interrupt(self):
        self._interrupted = True
        tasks = list(self._tools)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._thread and self._turn:
            await self._request("turn/interrupt", {"threadId": self._thread, "turnId": self._turn})
