"""Fixed offline-only entry point for the local macOS app bundle.

The native launcher selects a path; it never opens dataset contents. The
existing offline launcher applies and verifies confinement before processing.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(__file__).with_name("offline-config.json").read_text())
    source = Path(config["source"])
    if not (source / "karospace_agent/offline.py").is_file():
        raise RuntimeError("The configured source checkout is missing. Rebuild the offline app.")
    sys.path.insert(0, str(source))
    from karospace_agent.offline import launch

    # No provider selection, cloud authentication, login shell, or fallback.
    return launch(surface="app" if args.smoke_test else "worker", model=config["model"], runtime=config["python"],
                  input_path=None if args.smoke_test else args.input,
                  smoke_test="chat" if args.smoke_test else False)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Offline app failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
