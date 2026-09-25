"""macOS network-denial primitive and a synthetic, fail-closed capability check.

Running the standalone diagnostic does not change an existing app session.
offline.py uses this primitive with filesystem grants to launch the confined
offline app; see docs/design/offline-isolation.md.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
# Deny even loopback and Unix sockets: a local proxy could forward private data.
# Deny Mach service lookup too, so helpers cannot ask launchd/XPC services to
# make requests on their behalf. No system-wide firewall settings are changed.
PROFILE = "(version 1)(allow default)(deny network*)(deny mach-lookup)"


class IsolationUnavailable(RuntimeError):
    pass


def isolated_command(argv: list[str]) -> list[str]:
    """Build an inherited OS restriction with no unprotected fallback."""
    if sys.platform != "darwin" or not SANDBOX_EXEC.is_file():
        raise IsolationUnavailable("network_isolation_unsupported")
    if not argv or any(type(value) is not str or not value or "\0" in value for value in argv):
        raise ValueError("invalid_isolated_command")
    return [str(SANDBOX_EXEC), "-p", PROFILE, *argv]


@contextmanager
def _unix_listener(directory):
    # A real listener avoids mistaking a missing path for a tested denial.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(Path(directory) / "probe.sock"))
        server.listen(2)
        yield


def check_network_isolation() -> dict:
    """Verify actual OS denials using only synthetic addresses and a local file.

    A timeout, missing runtime, invalid report or ordinary connection failure
    is not proof of isolation. Only permission denials count as success.
    """
    checks = ("ipv4_tcp", "ipv6_tcp", "ipv4_udp", "ipv6_udp", "unix_socket")
    try:
        # Keep below macOS's short sockaddr_un path limit.
        with tempfile.TemporaryDirectory(prefix="karo-isolate-", dir="/private/tmp" if sys.platform == "darwin" else None) as directory:
            command = isolated_command([
                sys.executable, "-I", str(Path(__file__).with_name("isolation_probe.py")), directory,
            ])
            with _unix_listener(directory):
                completed = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
            report = json.loads(completed.stdout)
            expected = {"file_io", "child_file_io", *checks, *("child_" + key for key in checks)}
            if (completed.returncode != 0 or type(report) is not dict or set(report) != expected
                    or any(value is not True for value in report.values())):
                raise IsolationUnavailable("network_isolation_verification_failed")
    except IsolationUnavailable as exc:
        return {"available": False, "diagnostic": str(exc)}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"available": False, "diagnostic": "network_isolation_verification_failed"}
    return {"available": True, "diagnostic": "network_denial_verified", "checks": report}
