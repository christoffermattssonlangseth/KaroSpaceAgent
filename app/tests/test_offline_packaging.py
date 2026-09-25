"""The desktop bundle can dispatch only the existing confined offline route."""
import importlib.util
import json
from pathlib import Path

import pytest

from karospace_agent import offline


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    app = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("offline_bundle_launch", app / "packaging/offline_launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "offline_launch.py"))
    monkeypatch.setattr(module.sys, "path", list(module.sys.path))
    (tmp_path / "offline-config.json").write_text(json.dumps({
        "source": str(app), "python": "/synthetic/python", "model": "/synthetic/model"}))
    calls = []
    monkeypatch.setattr(offline, "launch", lambda **kwargs: calls.append(kwargs) or 0)
    return module, calls


def test_bundle_uses_fixed_runtime_and_confined_route(launcher, monkeypatch):
    module, calls = launcher
    path = "/synthetic/input with spaces.h5ad"
    monkeypatch.setattr(module.sys, "argv", ["offline_launch.py", "--input", path])
    assert module.main() == 0
    assert calls == [{"surface": "worker", "model": "/synthetic/model", "runtime": "/synthetic/python",
                      "input_path": path, "smoke_test": False}]


@pytest.mark.parametrize("arguments", [["--provider", "codex"], ["--model", "cloud-model"]])
def test_bundle_cannot_be_redirected_to_a_provider(launcher, monkeypatch, arguments):
    module, calls = launcher
    monkeypatch.setattr(module.sys, "argv", ["offline_launch.py", *arguments])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    assert calls == []


def test_bundle_smoke_test_never_opens_selected_research_data(launcher, monkeypatch):
    module, calls = launcher
    monkeypatch.setattr(module.sys, "argv", ["offline_launch.py", "--input", "/synthetic/unused.h5ad", "--smoke-test"])
    assert module.main() == 0
    assert calls[0]["input_path"] is None
    assert calls[0]["smoke_test"] == "chat"
    assert "workspace_parent" not in calls[0]  # Exercise the ordinary session location.
