"""Confined entry point: verify the current process before opening data/models."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys


def verify(config):
    from karospace_agent import isolation_probe
    report = isolation_probe.probe(config["probe_directory"])
    child = subprocess.run([sys.executable, "-I", str(Path(isolation_probe.__file__)),
                            config["probe_directory"], "--child"],
                           capture_output=True, text=True, timeout=15, check=False)
    child_report = json.loads(child.stdout)
    if (child.returncode or set(child_report) != set(report)
            or not all(value is True for value in (*report.values(), *child_report.values()))):
        raise RuntimeError("Current process or child network isolation could not be verified.")
    outside = Path(config["outside_probe"])
    for operation in (lambda: (outside / "read-test").read_bytes(),
                      lambda: (outside / "write-test").write_bytes(b"synthetic")):
        try:
            operation()
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES):
                continue
        raise RuntimeError("Current process filesystem isolation could not be verified.")


def main():
    os.umask(0o077)
    # -I ignores user PYTHONPATH/site customizations. This is the trusted app
    # directory, not a dataset directory chosen by a model.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    verify(config)
    from karospace_agent.offline_session import OfflineSession
    if config.get("smoke_test"):
        # Exercise the real runtime and local tool with entirely synthetic input.
        from karospace_agent.privacy import Boundary
        boundary = Boundary()
        if config["smoke_test"] != "tools":
            print("Offline check: network and filesystem confinement verified; loading local model.", flush=True)
            session = OfflineSession(config)
            print("Offline check: generating a synthetic CPU reply.", flush=True)
            if config["smoke_test"] == "chat":
                try:
                    reply = session.send("Do not use tools. Reply with a message saying Ready.")
                except RuntimeError:
                    # Developer-only synthetic request; diagnose protocol failures.
                    responses = [m["content"][:1000] for m in session.messages if m["role"] == "assistant"]
                    print(json.dumps({"synthetic_protocol_responses": responses[-2:]}), flush=True)
                    raise
            else:
                reply = session.model.complete([{"role": "user", "content": "Reply only with OK."}], max_tokens=4)
            if not reply.strip():
                raise RuntimeError("The local model did not produce a reply.")
            if config["smoke_test"] == "chat" and reply.strip().lower().rstrip(".! ") != "ready":
                raise RuntimeError("The local chat model did not follow the synthetic Ready request.")
            boundary = session.boundary
        print("Offline check: verifying local scientific tools.", flush=True)
        from karospace_agent import commands
        import anndata as ad
        import numpy as np
        path = Path(config["workspace"]) / "synthetic.h5ad"
        data = ad.AnnData(np.ones((3, 2), dtype=np.float32))
        data.obsm["spatial"] = np.zeros((3, 2))
        data.write_h5ad(path)
        checked = commands.run_readiness(str(path), str(path.parent), require_spatial=False)
        result = boundary.filter("check_readiness", {}, {}, {"_local": {
            "returncode": checked.returncode, "stdout": checked.stdout, "stderr": checked.stderr}})
        if result.get("is_error") or not json.loads(result["content"][0]["text"])["ready"]:
            raise RuntimeError("Local scientific tools failed inside isolation.")
        inspected = commands.run_karospace([str(path), "--inspect-input"], timeout=120)
        if not inspected.ok:
            raise RuntimeError("Installed KaroSpace CLI failed inside isolation: " + inspected.stderr[-1500:])
        if config["surface"] == "app":
            import tkinter as tk
            window = tk.Tk()
            window.withdraw()
            window.update()
            window.destroy()
        print(json.dumps({"offline_smoke_test": "passed", "local_model": config["smoke_test"] != "tools",
                          "local_tool": True, "network_denied": True, "filesystem_confined": True}))
        return 0
    if config["surface"] == "app":
        from karospace_agent.offline_ui import run_app
        run_app(config)
    else:
        from karospace_agent.offline_ui import run_chat
        run_chat(config)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # This terminal is local. Never retry in cloud or without confinement.
        print(f"Offline startup/session failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
