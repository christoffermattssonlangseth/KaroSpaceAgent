"""Command wrappers: paths resolve, and failures degrade gracefully to a
RunResult instead of raising (so the model always gets a readable error)."""

from karospace_agent import commands


def test_repo_root_points_at_the_agent_repo():
    # The merge script this repo ships must be discoverable from REPO_ROOT.
    assert (commands.REPO_ROOT / "scripts" / "merge_sections.py").exists()


def test_run_missing_executable_returns_127_not_raise():
    rr = commands.run(["definitely-not-a-real-binary-zzz", "--help"])
    assert rr.returncode == 127
    assert rr.ok is False
    assert "not found" in rr.stderr.lower()


def test_run_captures_exit_code_and_streams():
    rr = commands.run(["python", "-c", "import sys; print('out'); sys.exit(3)"])
    assert rr.returncode == 3
    assert "out" in rr.stdout
    assert rr.ok is False


def test_run_ok_true_on_zero_exit():
    rr = commands.run(["python", "-c", "print('hi')"])
    assert rr.ok is True
    assert "hi" in rr.stdout


def test_karospace_missing_reports_cleanly(monkeypatch):
    monkeypatch.setattr(commands, "karospace_bin", lambda: None)
    rr = commands.run_karospace(["x.h5ad", "--inspect-input"])
    assert rr.returncode == 127
    assert "karospace" in rr.stderr


def test_companion_missing_reports_cleanly(monkeypatch):
    monkeypatch.setattr(commands, "companion_bin", lambda: None)
    rr = commands.run_companion(["prepare", "x.h5ad"])
    assert rr.returncode == 127
    assert "companion" in rr.stderr.lower()


def test_run_merge_builds_repeated_section_flags(monkeypatch):
    captured = {}

    def fake_run(argv, timeout=0):
        captured["argv"] = argv
        return commands.RunResult(0, "", "")

    monkeypatch.setattr(commands, "run", fake_run)
    commands.run_merge(["P1_L:/a.h5ad:Lesional", "P1_NL:/b.h5ad:Non-lesional"], "/m.h5ad")
    argv = captured["argv"]
    assert argv.count("--section") == 2
    assert "P1_L:/a.h5ad:Lesional" in argv
    assert argv[-2:] == ["--output", "/m.h5ad"]
