"""Opt-in check of the installed Codex runtime against a local mock endpoint.

No model credential or real dataset is used. Run with KAROSPACE_TEST_CODEX=1.
Unlike a fake app-server, this catches ambient instructions added by Codex.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import threading

import pytest

from karospace_agent import codex


@pytest.mark.skipif(os.environ.get("KAROSPACE_TEST_CODEX") != "1" or not shutil.which("codex"),
                    reason="opt-in installed Codex / localhost check")
def test_real_codex_does_not_inherit_private_instructions(tmp_path, monkeypatch):
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.headers.get("Content-Encoding") == "zstd":
                import zstandard
                data = zstandard.ZstdDecompressor().decompress(data)
            captured.append(json.loads(data))
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"local probe complete"}}')

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    private_home = tmp_path / "private-home"
    private_home.mkdir()
    sentinel = "PRIVATE_PERSON_19700101"
    (private_home / "AGENTS.md").write_text(sentinel)
    (private_home / "config.toml").write_text(f'developer_instructions = "{sentinel}"\n')
    monkeypatch.setenv("CODEX_HOME", str(private_home))
    monkeypatch.delenv("KAROSPACE_CODEX_BIN", raising=False)
    original_config = codex.isolation_config
    def config():
        return original_config() | {
            "model_provider": "probe",
            "model_providers.probe": {"name": "probe", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                                       "wire_api": "responses", "requires_openai_auth": False},
        }
    monkeypatch.setattr(codex, "isolation_config", config)
    class Probe(codex.Session):
        async def _request(self, method, params, **kwargs):
            if method == "thread/start":
                params["modelProvider"] = "probe"
            return await super()._request(method, params, **kwargs)

    async def exercise():
        async with Probe(model="gpt-5.4", on_event=lambda *_: None) as session:
            draft = session.boundary.preview("Reply hello.")
            with pytest.raises(RuntimeError):
                await session.send(session.boundary.approve(draft["draft_id"]))
    try:
        asyncio.run(asyncio.wait_for(exercise(), 40))
        assert captured, "Installed Codex never contacted the local mock endpoint"
        wire = json.dumps(captured)
        assert sentinel not in wire
        assert str(private_home) not in wire
        assert str(Path.home()) not in wire
        assert '"Reply hello."' in wire
        assert "run_export" in wire
        assert "exec_command" not in wire
        assert "write_stdin" not in wire
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
