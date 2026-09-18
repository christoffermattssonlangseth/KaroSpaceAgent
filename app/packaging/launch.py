"""PyInstaller entry point for the bundled `.app`.

A double-clicked bundle has no argv, so this hard-codes the `app` subcommand —
the native-window face — and forwards its exit code. Everything else (PATH
rehydration, the environment preflight, the server + window) is the normal CLI
path in `karospace_agent.cli.main`.
"""

import sys

from karospace_agent.cli import main

if __name__ == "__main__":
    sys.exit(main(["app"]))
