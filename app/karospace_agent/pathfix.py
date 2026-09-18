"""Rehydrate PATH for a GUI-launched app.

A process started from Finder (a double-clicked `.app`) inherits a *bare*
environment: `PATH` is `/usr/bin:/bin:/usr/sbin:/sbin` and none of the user's
shell config has run. So `shutil.which("karospace")` — and the Agent SDK's own
lookup of the `claude` CLI — find nothing, even on a machine where everything is
installed. The app looks broken through no fault of the install.

The fix is to ask the user's *login shell* what PATH it would produce and merge
any missing entries into this process's PATH. We do this once, early, before any
tool lookup. It is a no-op in a normal terminal launch (the shell PATH is
already present) and safe to call unconditionally.

Nothing here reads data or talks to the model; it only touches PATH.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable

# Marker of a Finder/`.app` launch: PATH is exactly the bare system default (in
# some order). If the user's own bin dirs are already present we skip the probe.
_BARE_PATH_DIRS = {"/usr/bin", "/bin", "/usr/sbin", "/sbin"}


def _default_runner(shell: str) -> str | None:
    """Ask the login shell to print the PATH it configures. `-l -i` so it sources
    the profile/rc files that add Homebrew, conda, pyenv, cargo, etc."""
    try:
        out = subprocess.run(
            [shell, "-l", "-i", "-c", "printf %s \"$PATH\""],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def looks_bare(path: str | None) -> bool:
    """True when PATH holds only the system defaults — the Finder-launch tell."""
    entries = [p for p in (path or "").split(os.pathsep) if p]
    return bool(entries) and all(p in _BARE_PATH_DIRS for p in entries)


def hydrate_path(
    env: dict[str, str] | None = None,
    runner: Callable[[str], str | None] = _default_runner,
    force: bool = False,
) -> list[str]:
    """Merge the login shell's PATH into `env` (default: os.environ), appending any
    entries not already present. Returns the list of dirs added.

    No-op off POSIX, or when PATH already looks non-bare (a normal terminal run),
    unless `force=True`. `runner` is injectable for tests."""
    if os.name != "posix":
        return []
    env = os.environ if env is None else env
    current = env.get("PATH", "")
    if not force and not looks_bare(current):
        return []

    shell = env.get("SHELL") or "/bin/zsh"
    resolved = runner(shell)
    if not resolved:
        return []

    have = [p for p in current.split(os.pathsep) if p]
    have_set = set(have)
    added = [p for p in resolved.split(os.pathsep) if p and p not in have_set]
    if added:
        env["PATH"] = os.pathsep.join(have + added)
    return added
