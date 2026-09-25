"""Synthetic subprocess probe; no dataset or credential is opened here."""
from __future__ import annotations

import errno
import json
from pathlib import Path
import socket
import subprocess
import sys


def denied(family, kind, address):
    try:
        with socket.socket(family, kind) as client:
            client.settimeout(1)
            if kind == socket.SOCK_DGRAM:
                client.sendto(b"karospace-isolation-test", address)
            else:
                client.connect(address)
    except OSError as exc:
        return exc.errno in {errno.EPERM, errno.EACCES}
    return False


def probe(directory, child=False):
    # The parent supplies a real Unix listener; ENOENT must NOT pass.
    root = Path(directory)
    marker = root / ("child-marker" if child else "marker")
    marker.write_text("synthetic local IO", encoding="utf-8")
    return {
        "file_io": marker.read_text(encoding="utf-8") == "synthetic local IO",
        "ipv4_tcp": denied(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 9)),
        "ipv6_tcp": denied(socket.AF_INET6, socket.SOCK_STREAM, ("::1", 9)),
        "ipv4_udp": denied(socket.AF_INET, socket.SOCK_DGRAM, ("127.0.0.1", 9)),
        "ipv6_udp": denied(socket.AF_INET6, socket.SOCK_DGRAM, ("::1", 9)),
        "unix_socket": denied(socket.AF_UNIX, socket.SOCK_STREAM, str(root / "probe.sock")),
    }


def main():
    directory = sys.argv[1]
    child = len(sys.argv) == 3 and sys.argv[2] == "--child"
    report = probe(directory, child)
    if not child:
        # No second sandbox wrapper: prove the OS restrictions are inherited.
        result = subprocess.run([sys.executable, "-I", __file__, directory, "--child"],
                                capture_output=True, text=True, timeout=10, check=True)
        report.update({"child_" + key: value for key, value in json.loads(result.stdout).items()})
    print(json.dumps(report))
    return 0 if all(value is True for value in report.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
