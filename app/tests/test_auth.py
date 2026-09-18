"""Credential detection: precedence, policy flags, and that no secret is read."""

import json

import pytest

from karospace_agent import auth

ENV_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_PROFILE", "CLAUDE_CONFIG_DIR", "ANTHROPIC_CONFIG_DIR",
    *auth.CLOUD_VARS, *auth.FEDERATION_VARS,
)


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "anthropic"))
    monkeypatch.setattr(auth.sys, "platform", "linux")
    return tmp_path


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)


def test_nothing_found(clean):
    s = auth.detect()
    assert s.source is None and not s.ok
    assert "no credential" in s.detail


def test_api_key_wins_and_is_permitted(clean, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-secret")
    s = auth.detect()
    assert s.source == "api_key" and s.ok
    assert "secret" not in s.line()  # never echo a value


def test_empty_key_is_flagged_not_skipped(clean, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    s = auth.detect()
    assert s.source is None and "EMPTY" in s.detail


def test_cloud_provider_outranks_everything(clean, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    s = auth.detect()
    assert s.source == "cloud" and "Bedrock" in s.label and s.ok


def test_subscription_token_not_permitted(clean, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    s = auth.detect()
    assert s.source == "subscription_token" and not s.permitted and not s.ok


def test_claudeai_login_not_permitted_and_plan_reported(clean):
    _write(clean / "claude" / ".credentials.json",
           {"claudeAiOauth": {"accessToken": "SECRET", "subscriptionType": "max"}})
    s = auth.detect()
    assert s.source == "claudeai_login" and not s.permitted
    assert "(max)" in s.label and "SECRET" not in s.line()


def test_console_profile_permitted(clean):
    _write(clean / "anthropic" / "configs" / "default.json",
           {"version": "1.0", "authentication": {"type": "user_oauth"}})
    s = auth.detect()
    assert s.source == "console_profile" and s.ok and "default" in s.label


def test_active_config_selects_profile(clean):
    _write(clean / "anthropic" / "active_config", "lab\n")
    _write(clean / "anthropic" / "configs" / "lab.json", {"authentication": {"type": "user_oauth"}})
    assert "lab" in auth.detect().label


def test_user_oauth_profile_ranks_below_stored_login(clean):
    _write(clean / "anthropic" / "configs" / "default.json", {"authentication": {"type": "user_oauth"}})
    _write(clean / "claude" / ".credentials.json", {"claudeAiOauth": {"subscriptionType": "pro"}})
    assert auth.detect().source == "claudeai_login"


def test_named_profile_ranks_above_stored_login(clean, monkeypatch):
    _write(clean / "anthropic" / "configs" / "lab.json", {"authentication": {"type": "user_oauth"}})
    _write(clean / "claude" / ".credentials.json", {"claudeAiOauth": {}})
    monkeypatch.setenv("ANTHROPIC_PROFILE", "lab")
    assert auth.detect().source == "console_profile"


def test_missing_named_profile_is_not_a_fallthrough_to_default(clean, monkeypatch):
    _write(clean / "anthropic" / "configs" / "default.json", {"authentication": {"type": "user_oauth"}})
    monkeypatch.setenv("ANTHROPIC_PROFILE", "nope")
    assert auth.detect().source is None


def test_federation_profile_and_env(clean, monkeypatch):
    _write(clean / "anthropic" / "configs" / "default.json", {"authentication": {"type": "oidc_federation"}})
    assert auth.detect().source == "federation"
    (clean / "anthropic" / "configs" / "default.json").unlink()
    for v in auth.FEDERATION_VARS:
        monkeypatch.setenv(v, "x")
    s = auth.detect()
    assert s.source == "federation" and "environment" in s.label


def test_api_key_helper_detected_with_caveat(clean):
    _write(clean / "claude" / "settings.json", {"apiKeyHelper": "/usr/local/bin/key.sh"})
    s = auth.detect()
    assert s.source == "api_key_helper" and s.ok and "untested" in s.detail


def test_macos_without_files_is_undetermined_not_none(clean, monkeypatch):
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    s = auth.detect()
    assert s.source is None and s.permitted and "Keychain" in s.detail


def test_corrupt_files_are_ignored(clean):
    _write(clean / "claude" / ".credentials.json", "{not json")
    _write(clean / "anthropic" / "configs" / "default.json", "[]")
    assert auth.detect().source is None
