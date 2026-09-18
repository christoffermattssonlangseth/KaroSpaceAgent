"""PATH rehydration for a GUI/`.app` launch (no subprocess actually spawned —
the login-shell probe is injected)."""

import os

import pytest

from karospace_agent import pathfix

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX PATH semantics")


def test_looks_bare_detects_finder_launch():
    assert pathfix.looks_bare("/usr/bin:/bin:/usr/sbin:/sbin")
    assert pathfix.looks_bare("/bin:/usr/bin")           # order-independent
    assert not pathfix.looks_bare("/opt/homebrew/bin:/usr/bin")
    assert not pathfix.looks_bare("")                    # empty is not "bare"


def test_hydrate_appends_missing_login_shell_dirs():
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "SHELL": "/bin/zsh"}
    added = pathfix.hydrate_path(
        env=env,
        runner=lambda shell: "/opt/homebrew/bin:/usr/bin:/Users/me/.cargo/bin",
    )
    assert added == ["/opt/homebrew/bin", "/Users/me/.cargo/bin"]
    # Existing entries are kept and not duplicated; new ones are appended.
    assert env["PATH"].split(os.pathsep) == [
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        "/opt/homebrew/bin", "/Users/me/.cargo/bin",
    ]


def test_hydrate_is_noop_when_path_already_rich():
    env = {"PATH": "/opt/homebrew/bin:/usr/bin", "SHELL": "/bin/zsh"}
    called = []
    added = pathfix.hydrate_path(env=env, runner=lambda shell: called.append(shell) or "x")
    assert added == [] and called == []  # not even probed
    assert env["PATH"] == "/opt/homebrew/bin:/usr/bin"


def test_hydrate_force_probes_even_when_rich():
    env = {"PATH": "/usr/bin", "SHELL": "/bin/zsh"}
    added = pathfix.hydrate_path(env=env, runner=lambda shell: "/usr/bin:/new/bin", force=True)
    assert added == ["/new/bin"]


def test_hydrate_survives_probe_failure():
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "SHELL": "/bin/zsh"}
    assert pathfix.hydrate_path(env=env, runner=lambda shell: None) == []
    assert env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"  # unchanged
