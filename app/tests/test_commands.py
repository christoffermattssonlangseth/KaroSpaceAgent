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


def test_run_streams_lines_to_on_line_sink():
    seen = []
    rr = commands.run(
        ["python", "-c", "import sys; print('a'); print('b'); print('e', file=sys.stderr)"],
        on_line=lambda stream, line: seen.append((stream, line.rstrip())),
    )
    assert rr.ok
    assert ("stdout", "a") in seen and ("stdout", "b") in seen
    assert ("stderr", "e") in seen
    # The captured text is unaffected by where the sink sends it.
    assert rr.stdout == "a\nb\n"


def test_set_progress_sink_installs_and_resets(monkeypatch):
    seen = []
    commands.set_progress_sink(lambda s, l: seen.append(l))
    try:
        commands.run(["python", "-c", "print('hi')"])
    finally:
        commands.set_progress_sink(None)
    assert seen == ["hi\n"]
    # Reset -> default: console when streaming on, silent when off.
    monkeypatch.setenv("KAROSPACE_AGENT_STREAM", "0")
    assert commands.get_progress_sink() is commands.null_sink
    monkeypatch.setenv("KAROSPACE_AGENT_STREAM", "1")
    assert commands.get_progress_sink() is commands.console_sink


def test_broken_sink_never_breaks_the_run():
    def boom(stream, line):
        raise RuntimeError("sink died")

    rr = commands.run(["python", "-c", "print('still ok')"], on_line=boom)
    assert rr.ok and "still ok" in rr.stdout
