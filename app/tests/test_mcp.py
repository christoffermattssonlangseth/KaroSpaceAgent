"""Exercise the real stdio transport, without a model or scientific datasets."""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from karospace_agent import tools


def test_stdio_tools_sanitize_validate_and_preserve_protocol(tmp_path, caplog):
    # The fixture emits both inspect values and normal export progress. Neither
    # may become stray stdout on the MCP transport; inspect values must also be
    # absent from the tool result.
    fake_cli = tmp_path / "karospace"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--inspect-input' in sys.argv:\n"
        "    print('obs columns:')\n"
        "    print('  - sample [categorical; 2 values] examples: PRIVATE_VALUE')\n"
        "elif '--fail' in sys.argv:\n"
        "    print('synthetic export failure', file=sys.stderr)\n"
        "    sys.exit(2)\n"
        "else:\n"
        "    print('export progress on stdout')\n"
        "    print('export progress on stderr', file=sys.stderr)\n"
    )
    fake_cli.chmod(0o755)
    artifact = tmp_path / "viewer.html"
    artifact.write_text("PRIVATE_ARTIFACT_CONTENT")

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "karospace_agent.mcp"],
            cwd=str(tmp_path),  # The server must work outside the repo root.
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "KAROSPACE_BIN": str(fake_cli),
                "KAROSPACE_AGENT_STREAM": "1",  # Server must override teeing.
            },
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert initialized.model_dump(by_alias=True)["serverInfo"]["name"] == "karospace"
                assert "schema" in initialized.instructions
                listed = await client.list_tools()
                assert {t.name for t in listed.tools} == set(tools.TOOL_NAMES)
                assert len(listed.tools) == 15
                for tool in listed.tools:
                    assert tool.model_dump(by_alias=True)["inputSchema"]["type"] == "object"

                inspected = await client.call_tool(
                    "inspect_input",
                    {"input_path": "synthetic.h5ad", "spatialdata_table": ""},
                )
                assert not inspected.model_dump(by_alias=True)["isError"]
                text = inspected.model_dump_json()
                assert "col_1" in text and "cardinality" in text
                assert "PRIVATE_VALUE" not in text
                assert "examples:" not in text

                exported = await client.call_tool(
                    "run_export",
                    {"input_path": "synthetic.h5ad", "output": "out.html", "flags": []},
                )
                assert not exported.model_dump(by_alias=True)["isError"]
                assert "export progress on stdout" not in exported.model_dump_json()
                assert "export progress on stderr" not in exported.model_dump_json()

                validated = await client.call_tool(
                    "validate_output", {"paths": [str(artifact), str(tmp_path / "missing")]}
                )
                assert not validated.model_dump(by_alias=True)["isError"]
                assert "false" in validated.model_dump_json()
                assert "PRIVATE_ARTIFACT_CONTENT" not in validated.model_dump_json()

                failed = await client.call_tool(
                    "run_export",
                    {"input_path": "synthetic.h5ad", "output": "out.html", "flags": ["--fail"]},
                )
                assert failed.model_dump(by_alias=True)["isError"]
                assert "synthetic export failure" not in failed.model_dump_json()
                assert "operation_failed" in failed.model_dump_json()
                invalid = await client.call_tool("validate_output", {"paths": "not-an-array"})
                assert invalid.model_dump(by_alias=True)["isError"]
                unknown = await client.call_tool("not_a_tool", {})
                assert unknown.model_dump(by_alias=True)["isError"]
                # The same connection remains usable after error results.
                assert len((await client.list_tools()).tools) == 15

    with caplog.at_level(logging.ERROR):
        asyncio.run(asyncio.wait_for(exercise(), timeout=30))
    # The MCP client's stdout decoder logs malformed protocol lines as errors.
    assert not caplog.records, [record.message for record in caplog.records]


def test_stdout_contains_only_jsonrpc(tmp_path):
    """A subprocess log must never be mistaken for an MCP protocol message."""
    import subprocess

    # Complete one handshake using raw pipes to independently check the wire.
    proc = subprocess.Popen(
        [sys.executable, "-m", "karospace_agent.mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "wire-test", "version": "1"},
            },
        }
        stdout, stderr = proc.communicate(json.dumps(request) + "\n", timeout=10)
        assert proc.returncode == 0, stderr
        messages = [json.loads(line) for line in stdout.splitlines()]
        assert any(message.get("id") == 1 and "result" in message for message in messages)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
