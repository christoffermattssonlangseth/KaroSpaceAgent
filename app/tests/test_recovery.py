"""Resume only unchanged, verified local inputs/outputs; never raw history."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from karospace_agent import commands, recovery
from karospace_agent.history import RunHistory
from karospace_agent.privacy import Boundary


PRIVATE = "PRIVATE_PATIENT_19700101"


def complete(tmp_path):
    from test_readiness import dataset
    source, output = tmp_path / (PRIVATE + ".h5ad"), tmp_path / "finished.h5ad"
    dataset(source)
    store = RunHistory()
    record = store.begin("qc_filter", {"input_path": str(source), "output": str(output),
                                      "min_counts": 1, "min_genes": 0})
    dataset(output)
    store.finish(record, "completed")
    return record, source, output


def test_completed_checkpoint_is_reused_after_restart_without_private_context(tmp_path):
    record, _, output = complete(tmp_path)
    boundary = Boundary()
    boundary.output_root = tmp_path / "new-output"
    draft = recovery.prepare(boundary, record)
    assert PRIVATE not in draft["text"] and str(tmp_path) not in draft["text"]
    assert "do not repeat" in draft["text"]
    alias = next(a for a, value in boundary.aliases.items() if value == str(output))
    assert alias in draft["text"]
    assert boundary.consume(boundary.approve(draft["draft_id"])) == draft["text"]


def test_changed_output_rejected_even_when_size_and_mtime_match(tmp_path):
    record, _, output = complete(tmp_path)
    stat = output.stat()
    with output.open("r+b") as stream:
        stream.seek(-1, 2)
        old = stream.read(1)
        stream.seek(-1, 2)
        stream.write(bytes([old[0] ^ 1]))
    os.utime(output, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(recovery.RecoveryError, match="changed_or_unverified"):
        recovery.prepare(Boundary(), record)


def test_incomplete_run_gets_exact_parameters_and_fresh_output(tmp_path):
    record, source, output = complete(tmp_path)
    record["status"] = "interrupted"
    original = output.read_bytes()
    boundary = Boundary()
    boundary.output_root = tmp_path / "retry"
    draft = recovery.prepare(boundary, record)
    assert PRIVATE not in draft["text"] and str(tmp_path) not in draft["text"]
    start = draft["text"].index('{"tool"')
    call, _ = json.JSONDecoder().raw_decode(draft["text"][start:])
    decoded = boundary.decode(call["arguments"])
    assert decoded["input_path"] == str(source)
    assert decoded["min_counts"] == 1 and decoded["min_genes"] == 0
    assert decoded["output"] != str(output)
    assert output.read_bytes() == original


@pytest.mark.parametrize("problem", ["changed", "missing", "legacy", "active"])
def test_unsafe_retries_are_rejected(tmp_path, problem):
    record, source, _ = complete(tmp_path)
    record["status"] = "interrupted"
    if problem == "changed":
        source.write_bytes(b"changed")
    elif problem == "missing":
        source.unlink()
    elif problem == "legacy":
        record.pop("input_fingerprints")
    else:
        record["status"] = "started"
        record["owner_pid"] = os.getpid()
    with pytest.raises(recovery.RecoveryError):
        recovery.prepare(Boundary(), record)


def test_truncated_dataset_is_not_reused_even_with_matching_digest(tmp_path):
    record, _, output = complete(tmp_path)
    output.write_bytes(b"not a dataset")
    record["output_fingerprints"] = [recovery.fingerprint(output)]
    with pytest.raises(recovery.RecoveryError, match="checkpoint_not_ready"):
        recovery.prepare(Boundary(), record)


def test_directory_fingerprint_tracks_nested_contents_and_rejects_links(tmp_path):
    store = tmp_path / "data.zarr"
    store.mkdir()
    chunk = store / "chunk"
    chunk.write_bytes(b"abc")
    saved = recovery.fingerprint(store)
    assert recovery.matches(saved)
    chunk.write_bytes(b"abd")
    assert not recovery.matches(saved)
    (store / "link").symlink_to(chunk)
    with pytest.raises(recovery.RecoveryError):
        recovery.fingerprint(store)


def test_companion_retry_rewrites_only_explicit_output(tmp_path):
    source = tmp_path / "source.h5ad"
    source.write_bytes(b"synthetic")
    record = RunHistory().begin("run_companion", {"args": ["prepare", str(source), "--groupby", PRIVATE,
                                                          "--output", str(tmp_path / "old.h5ad")]})
    record["status"] = "error"
    boundary = Boundary()
    boundary.output_root = tmp_path / "retry"
    draft = recovery.prepare(boundary, record)
    assert PRIVATE not in draft["text"]
    start = draft["text"].index('{"tool"')
    call, _ = json.JSONDecoder().raw_decode(draft["text"][start:])
    args = boundary.decode(call["arguments"])["args"]
    assert args[:4] == ["prepare", str(source), "--groupby", PRIVATE]
    assert args[-1] != str(tmp_path / "old.h5ad")


def test_recovery_route_requires_local_review_before_any_send(tmp_path):
    from starlette.testclient import TestClient
    from karospace_agent.web import create_app
    from test_web import FakeSession
    record, _, _ = complete(tmp_path)
    app = create_app(session_factory=FakeSession)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 123)) as client:
        body = {"session_id": record["session_id"], "run_id": record["id"]}
        assert client.post("/recovery/preview", json=body).status_code == 403
        response = client.post("/recovery/preview", json=body, headers={"X-KaroSpace-Local": "1"})
        assert response.status_code == 200
        assert PRIVATE not in response.text and str(tmp_path) not in response.text
        assert app.state.conversation._session.sent == []
        assert client.post("/send", json={"draft_id": response.json()["draft_id"]}).status_code == 202


def test_changed_checkpoint_after_preview_cannot_be_sent(tmp_path):
    from starlette.testclient import TestClient
    from karospace_agent.web import create_app
    from test_web import FakeSession
    record, _, output = complete(tmp_path)
    app = create_app(session_factory=FakeSession)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 123)) as client:
        response = client.post("/recovery/preview", headers={"X-KaroSpace-Local": "1"},
                               json={"session_id": record["session_id"], "run_id": record["id"]})
        assert response.status_code == 200
        output.write_bytes(b"changed after preview")
        token = response.json()["draft_id"]
        assert client.post("/send", json={"draft_id": token}).status_code == 409
        assert client.post("/send", json={"draft_id": token}).status_code == 400
        assert app.state.conversation._session.sent == []


def test_recovery_aliases_decode_once_without_recursive_expansion():
    boundary = Boundary()
    first = boundary.alias("param_2", "param")
    boundary.alias(PRIVATE, "param")
    assert boundary.decode({"flags": [first]}) == {"flags": ["param_2"]}


def test_completed_html_is_not_treated_as_a_complete_sidecar_bundle(tmp_path):
    source = tmp_path / "source.h5ad"
    source.write_bytes(b"synthetic")
    store = RunHistory()
    record = store.begin("run_export", {"input_path": str(source), "output": str(tmp_path / "viewer.html"), "flags": []})
    store.finish(record, "completed")
    with pytest.raises(recovery.RecoveryError, match="no_dataset_checkpoint"):
        recovery.prepare(Boundary(), record)
