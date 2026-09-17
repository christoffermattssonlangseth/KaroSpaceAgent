"""The boundary is only as good as these guarantees, so pin them down."""

import os

from karospace_agent.sanitize import (
    MAX_OUTPUT_CHARS,
    stat_path,
    stat_paths,
    strip_inspect_examples,
    truncate,
)


def test_truncate_passthrough_when_short():
    assert truncate("hello") == "hello"


def test_truncate_caps_long_output_and_keeps_head_and_tail():
    text = "H" * 1000 + "M" * 100_000 + "T" * 1000
    out = truncate(text)
    assert len(out) < len(text)
    assert out.startswith("H")
    assert out.endswith("T")
    assert "truncated" in out
    # The visible portion never exceeds the cap (plus the short marker line).
    assert len(out) <= MAX_OUTPUT_CHARS + 100


def test_truncate_handles_none():
    assert truncate(None) == ""


def test_stat_path_missing():
    s = stat_path("/no/such/path/xyzzy.h5ad")
    assert s.exists is False
    assert s.kind == "missing"
    assert s.size_bytes is None
    assert "MISSING" in s.human()


def test_stat_path_file(tmp_path):
    f = tmp_path / "viewer.html"
    f.write_text("x" * 42)
    s = stat_path(str(f))
    assert s.exists and s.kind == "file"
    assert s.size_bytes == 42


def test_stat_path_dir_sums_contents(tmp_path):
    d = tmp_path / "viewer.features"
    d.mkdir()
    (d / "shard0.bin").write_bytes(b"\x00" * 100)
    (d / "shard1.bin").write_bytes(b"\x00" * 50)
    s = stat_path(str(d))
    assert s.kind == "dir"
    assert s.size_bytes == 150


def test_stat_paths_table_lists_each(tmp_path):
    f = tmp_path / "viewer.html"
    f.write_text("hi")
    table = stat_paths([str(f), "/missing/thing"])
    assert str(f) in table
    assert "MISSING" in table
    assert len(table.splitlines()) == 2


def test_strip_inspect_examples_removes_value_tail_keeps_schema():
    report = (
        "obs columns:\n"
        '  - x_centroid [numeric; 38,270 values] examples: "1101.86", "982.4"\n'
        '  - sample_id [categorical; 2 values] examples: "P1_L", "P1_NL"\n'
        "  - orig.ident [categorical; 1 values] examples: SeuratProject\n"
    )
    out = strip_inspect_examples(report)
    # No literal value survives.
    assert "1101.86" not in out
    assert "P1_L" not in out
    assert "SeuratProject" not in out
    assert "examples:" not in out
    # Schema is fully preserved.
    assert "x_centroid [numeric; 38,270 values]" in out
    assert "sample_id [categorical; 2 values]" in out
    assert "orig.ident [categorical; 1 values]" in out
    assert "obs columns:" in out


def test_strip_inspect_examples_leaves_lines_without_examples():
    text = "Feature counts:\n  rna: 5,001 genes\n  protein: 12 proteins"
    assert strip_inspect_examples(text) == text


def test_no_tool_reads_file_bytes():
    """Regression guard: sanitize exposes stat, never a byte-reading helper."""
    import karospace_agent.sanitize as s

    for name in dir(s):
        assert "read" not in name.lower() or name.startswith("__"), name
