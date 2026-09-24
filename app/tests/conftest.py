"""Keep synthetic run histories out of the user's persistent app history."""
import pytest


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    monkeypatch.setenv("KAROSPACE_AGENT_HISTORY_DIR", str(tmp_path / "history"))
