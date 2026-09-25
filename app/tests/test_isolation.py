"""OS isolation must be measured, never inferred from a launch flag."""
import errno
from contextlib import nullcontext
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from karospace_agent import cli, isolation, isolation_probe


def success_report():
    checks = ("file_io", "ipv4_tcp", "ipv6_tcp", "ipv4_udp", "ipv6_udp", "unix_socket")
    return {key: True for name in checks for key in (name, "child_" + name)}


def fake_runtime(monkeypatch):
    monkeypatch.setattr(isolation.sys, "platform", "darwin")
    monkeypatch.setattr(isolation, "SANDBOX_EXEC", Path(__file__))
    monkeypatch.setattr(isolation, "_unix_listener", lambda directory: nullcontext())


def test_launch_uses_fixed_profile_without_shell_or_fallback(monkeypatch):
    fake_runtime(monkeypatch)
    argv = ["python", "file with spaces.py", "$(must stay literal)"]
    command = isolation.isolated_command(argv)
    assert command[:3] == [str(Path(__file__)), "-p", isolation.PROFILE]
    assert command[3:] == argv
    assert "(deny network*)" in command[2] and "(deny mach-lookup)" in command[2]


def test_unsupported_platform_fails_closed(monkeypatch):
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    assert isolation.check_network_isolation() == {
        "available": False, "diagnostic": "network_isolation_unsupported"}


@pytest.mark.parametrize("payload,returncode", [
    ("PRIVATE traceback", 0), (json.dumps(success_report()), 1),
    (json.dumps(success_report() | {"unix_socket": False}), 0),
    (json.dumps(success_report() | {"child_ipv4_tcp": 1}), 0),
    (json.dumps(success_report() | {"unexpected": "PRIVATE"}), 0),
    ('{"available": true}', 0),
])
def test_missing_or_malformed_proof_never_reports_isolation(monkeypatch, payload, returncode):
    fake_runtime(monkeypatch)
    monkeypatch.setattr(isolation.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=returncode, stdout=payload, stderr="PRIVATE"))
    result = isolation.check_network_isolation()
    assert not result["available"] and "PRIVATE" not in json.dumps(result)


def test_valid_proof_requires_every_parent_and_child_denial(monkeypatch):
    fake_runtime(monkeypatch)
    monkeypatch.setattr(isolation.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=json.dumps(success_report())))
    assert isolation.check_network_isolation()["available"] is True


@pytest.mark.parametrize("error,expected", [(errno.EPERM, True), (errno.EACCES, True),
    (errno.ECONNREFUSED, False), (errno.ENOENT, False), (errno.ETIMEDOUT, False),
    (errno.EAFNOSUPPORT, False)])
def test_only_permission_denial_is_evidence(monkeypatch, error, expected):
    def blocked(*args):
        raise OSError(error, "synthetic")
    monkeypatch.setattr(isolation_probe.socket, "socket", blocked)
    assert isolation_probe.denied(0, 0, None) is expected


def test_cli_check_never_starts_auth_or_a_model(monkeypatch, capsys):
    monkeypatch.setattr(isolation, "check_network_isolation", lambda: {"available": True})
    monkeypatch.setattr(cli, "_preflight", lambda *a: pytest.fail("cloud preflight must not run"))
    assert cli.main(["isolation-check"]) == 0
    assert json.loads(capsys.readouterr().out)["available"]


@pytest.mark.skipif(os.environ.get("KAROSPACE_TEST_OS_ISOLATION") != "1",
                    reason="Opt-in real OS probe; requires permission to create a macOS sandbox")
def test_real_os_denies_network_and_descendants_but_allows_local_files():
    assert isolation.check_network_isolation()["available"] is True
