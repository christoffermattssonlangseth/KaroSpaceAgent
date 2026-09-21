"""The GEO fetcher's boundary-relevant, network-free surface: platform
inference, manifest formatting (public catalogue facts only), the assembler
dispatch table, and the command-wrapper argv. The h5py/remotezip/anndata reads
and every HTTP call run only under the karospace interpreter and against the
network, so these tests exercise the pure routing/formatting by loading the
script directly and stubbing nothing that touches the wire."""

import importlib.util

from karospace_agent import commands


def load_geo():
    spec = importlib.util.spec_from_file_location("geo_fetch", commands.GEO_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


geo = load_geo()


def test_geo_script_is_discoverable_from_repo_root():
    assert commands.GEO_SCRIPT.exists()


def test_infer_platform_from_filenames():
    assert geo.infer_platform(["GSM_x_outs.zip"], "Xenium") == "xenium"
    assert geo.infer_platform(["a_filtered_feature_bc_matrix.h5", "a_tissue_positions.csv.gz"], "") == "visium"
    assert geo.infer_platform(["GSM_cell_by_gene.csv.gz", "GSM_cell_metadata.csv.gz"], "MERSCOPE") == "merscope"
    assert geo.infer_platform(["binned_outputs.tar"], "") == "visium_hd"
    assert geo.infer_platform(["sample_filtered_feature_bc_matrix.h5"], "Chromium") == "chromium"
    assert geo.infer_platform(["readme.txt"], "") == "unknown"


def test_all_three_assemblers_registered():
    assert set(geo.ASSEMBLERS) == {"xenium", "visium", "merscope"}
    for p in geo.ASSEMBLERS:
        assert p in geo.BUILD_NEEDS


def test_find_file_matches_all_substrings_and_reports_missing():
    sample = {"gsm": "GSM1", "files": [
        {"name": "GSM1_tissue_positions.csv.gz", "url": "u1"},
        {"name": "GSM1_filtered_feature_bc_matrix.h5", "url": "u2"},
    ]}
    assert geo._find_file(sample, "tissue_positions")["url"] == "u1"
    assert geo._find_file(sample, "filtered_feature_bc_matrix", ".h5")["url"] == "u2"
    assert geo._find_file(sample, "cell_by_gene") is None


def test_fetch_file_raises_with_filenames_when_layout_unfamiliar(tmp_path):
    sample = {"gsm": "GSM1", "files": [{"name": "GSM1_everything.tar", "url": "u"}]}
    try:
        geo._fetch_file(sample, tmp_path, [], "tissue_positions", role="positions")
        assert False, "expected a loud failure"
    except ValueError as e:
        assert "positions" in str(e)
        assert "GSM1_everything.tar" in str(e)  # tells you what WAS there


def test_ftp_urls_upgraded_to_range_capable_https():
    assert geo._ftp_to_https("ftp://ftp.ncbi.nlm.nih.gov/x").startswith("https://")
    # already-https is left alone
    assert geo._ftp_to_https("https://ftp.ncbi.nlm.nih.gov/x") == "https://ftp.ncbi.nlm.nih.gov/x"


def test_human_size_is_readable():
    assert geo._human(None) == "?"
    assert geo._human(9_861_155_708).endswith("GB")
    assert geo._human(133) == "133 B"


def test_xenium_outs_url_picks_the_bundle():
    sample = {"files": [
        {"name": "GSM_he_image.ome.tif.gz", "url": "https://h/img"},
        {"name": "GSM_Rep1_outs.zip", "url": "https://h/outs"},
    ]}
    assert geo._xenium_outs_url(sample) == "https://h/outs"
    assert geo._xenium_outs_url({"files": [{"name": "x.tif", "url": "u"}]}) is None


def test_format_manifest_shows_platform_files_and_build_hint():
    m = {
        "accession": "GSE1",
        "title": "A study",
        "summary": "why",
        "n_samples": 1,
        "samples": [{
            "gsm": "GSM1",
            "title": "Rep1",
            "organism": "Homo sapiens",
            "instrument": "Xenium",
            "platform": "xenium",
            "files": [{"name": "GSM1_Rep1_outs.zip", "url": "u", "size_bytes": 9_000_000_000}],
        }],
    }
    out = geo.format_manifest(m)
    assert "GSE1" in out and "GSM1  [xenium]" in out
    assert "GSM1_Rep1_outs.zip" in out and "GB" in out
    assert "build pulls only:" in out and "cell_feature_matrix.h5" in out
    # Only public catalogue facts — no cell values leak into a manifest.
    assert "x_centroid" not in out


def test_read_positions_headerless_and_headered(tmp_path):
    import pandas as pd

    # Older Space Ranger: headerless, 6 columns.
    p1 = tmp_path / "pos_old.csv"
    p1.write_text("AAA-1,1,0,0,100,200\nBBB-1,1,1,1,110,210\n")
    df1 = geo._read_positions(p1)
    assert list(df1.index) == ["AAA-1", "BBB-1"]
    assert df1.loc["AAA-1", "pxl_col_in_fullres"] == 200

    # Newer Space Ranger: a header row.
    p2 = tmp_path / "pos_new.csv"
    p2.write_text("barcode,in_tissue,array_row,array_col,pxl_row_in_fullres,pxl_col_in_fullres\n"
                  "AAA-1,1,0,0,100,200\n")
    df2 = geo._read_positions(p2)
    assert list(df2.index) == ["AAA-1"]
    assert df2.loc["AAA-1", "pxl_row_in_fullres"] == 100


def test_unsupported_platform_reports_needs(monkeypatch, tmp_path):
    # A platform with no assembler must fail with a helpful message, not crash.
    monkeypatch.setattr(geo, "build_manifest", lambda acc, sizes=False: {
        "accession": acc, "title": "", "summary": "", "n_samples": 1,
        "samples": [{"gsm": "GSM9", "platform": "visium_hd", "files": []}],
    })
    try:
        geo.run_build("GSE9", ["GSM9"], "visium_hd", tmp_path / "o.h5ad", tmp_path, False)
        assert False, "expected NotImplemented-style error"
    except ValueError as e:
        assert "not yet implemented" in str(e)
        assert "tissue_positions.parquet" in str(e)  # tells you what it would need


def test_build_argv_carries_gsms_and_flags(monkeypatch):
    captured = {}

    def fake_run(argv, timeout=0):
        captured["argv"] = argv
        return commands.RunResult(0, "", "")

    monkeypatch.setattr(commands, "run", fake_run)
    monkeypatch.setattr(commands, "merge_python", lambda: "PY")
    commands.run_geo_build("GSE1", ["GSM1", "GSM2"], "xenium", "out.h5ad", include_control=True)
    argv = captured["argv"]
    assert argv[:4] == ["PY", str(commands.GEO_SCRIPT), "build", "GSE1"]
    assert "--platform" in argv and "xenium" in argv
    assert argv.count("--gsm") == 2 and "GSM1" in argv and "GSM2" in argv
    assert "--include-control-features" in argv
    assert "-o" in argv and "out.h5ad" in argv
