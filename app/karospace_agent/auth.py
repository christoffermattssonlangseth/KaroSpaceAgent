"""Which credential the model will run under, and whether it is one this app
may use.

The Agent SDK spawns the Claude Code CLI, which picks a credential in a fixed
order (cloud provider, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY, apiKeyHelper,
CLAUDE_CODE_OAUTH_TOKEN, Anthropic profile, stored `/login`). This module
mirrors that order from the *outside* — env vars and the presence/shape of the
files the CLI reads — so we can tell the user up front which one will apply,
and stop them before a run that would fail or would break Anthropic's terms.

Policy, from the Agent SDK docs: "Unless previously approved, Anthropic does
not allow third party developers to offer claude.ai login or rate limits for
their products, including agents built on the Claude Agent SDK." So a claude.ai
subscription login (Pro/Max/Team/Enterprise, or a `claude setup-token`) is
flagged NOT PERMITTED here. The sanctioned routes are a Console sign-in
(a Claude Code `/login` with the Console account, which stores an Anthropic
profile), an API key, Workload Identity Federation, or a cloud provider.

Nothing here reads a secret value. Env vars are tested for presence; files
are opened only to read their top-level key names or a non-secret `type`.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

CLOUD_VARS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
FEDERATION_VARS = (
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
)

CONSOLE_SIGNIN_HELP = """\
To sign in without pasting a key (recommended):
  1. Install Claude Code if you haven't:  npm install -g @anthropic-ai/claude-code
  2. Run `claude`, choose "Anthropic Console account", then
     "Sign in with your Console account (recommended)" and finish in the browser.
     (Unset ANTHROPIC_API_KEY first if you have one exported.)
  3. Done — karospace-agent picks that login up automatically.
Alternatively export an API key from https://platform.claude.com:
  export ANTHROPIC_API_KEY=sk-ant-...
Note: a claude.ai (Pro/Max) login is NOT permitted for this app — Anthropic's
terms reserve claude.ai logins for Claude Code and claude.ai themselves."""


@dataclass
class AuthStatus:
    source: str | None  # machine-readable id, None when nothing was found
    label: str          # what to show the user
    permitted: bool     # allowed for an Agent SDK app under Anthropic's terms
    detail: str = ""    # a caveat, never a secret

    @property
    def ok(self) -> bool:
        return self.source is not None and self.permitted

    def line(self) -> str:
        """One line for a banner: `auth: <label>[ — <detail>]`."""
        tail = f" — {self.detail}" if self.detail else ""
        return f"auth: {self.label}{tail}"


def _set(name: str) -> bool:
    return bool(os.environ.get(name))


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def anthropic_config_dir() -> Path:
    if os.environ.get("ANTHROPIC_CONFIG_DIR"):
        return Path(os.environ["ANTHROPIC_CONFIG_DIR"])
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "Anthropic"
    return Path.home() / ".config" / "anthropic"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _profile_name() -> str | None:
    """Named profile, else the active one, else 'default' — if its file exists."""
    cfg = anthropic_config_dir()

    def usable(name: str) -> bool:
        return _read_json(cfg / "configs" / f"{name}.json") is not None

    named = os.environ.get("ANTHROPIC_PROFILE")
    if named:
        return named if usable(named) else None
    active = None
    try:
        active = (cfg / "active_config").read_text("utf-8").strip() or None
    except OSError:
        pass
    for candidate in (active, "default"):
        if candidate and usable(candidate):
            return candidate
    return None


def _profile_auth_type(name: str) -> str:
    data = _read_json(anthropic_config_dir() / "configs" / f"{name}.json") or {}
    auth = data.get("authentication")
    return str(auth.get("type", "")) if isinstance(auth, dict) else ""


def _stored_login() -> AuthStatus | None:
    """A Claude Code `/login` on this machine, judged by the credential file's
    key names only."""
    creds = _read_json(claude_config_dir() / ".credentials.json")
    if creds is None:
        return None
    if "claudeAiOauth" in creds:
        oauth = creds.get("claudeAiOauth")
        plan = oauth.get("subscriptionType") if isinstance(oauth, dict) else None
        plan_txt = f" ({plan})" if isinstance(plan, str) and plan else ""
        return AuthStatus(
            "claudeai_login",
            f"claude.ai login{plan_txt}",
            permitted=False,
            detail="claude.ai logins are not permitted for third-party Agent SDK apps",
        )
    if creds:
        return AuthStatus("stored_credential", "stored Claude Code credential", permitted=True)
    return None


def detect() -> AuthStatus:
    """Which credential the CLI subprocess will use, in its precedence order."""
    if any(_truthy(v) for v in CLOUD_VARS):
        which = ", ".join(v.removeprefix("CLAUDE_CODE_USE_").title() for v in CLOUD_VARS if _truthy(v))
        return AuthStatus("cloud", f"cloud provider ({which})", permitted=True)
    if _set("ANTHROPIC_AUTH_TOKEN"):
        return AuthStatus("auth_token", "bearer token (ANTHROPIC_AUTH_TOKEN)", permitted=True)
    if _set("ANTHROPIC_API_KEY"):
        return AuthStatus("api_key", "API key (ANTHROPIC_API_KEY)", permitted=True)
    if "ANTHROPIC_API_KEY" in os.environ or "ANTHROPIC_AUTH_TOKEN" in os.environ:
        return AuthStatus(
            None,
            "none",
            permitted=False,
            detail="ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN is set but EMPTY; unset it rather than blanking it",
        )
    settings = _read_json(claude_config_dir() / "settings.json") or {}
    if settings.get("apiKeyHelper"):
        return AuthStatus(
            "api_key_helper",
            "apiKeyHelper script (user settings)",
            permitted=True,
            detail="untested with this app, which loads no settings files",
        )
    if _set("CLAUDE_CODE_OAUTH_TOKEN"):
        return AuthStatus(
            "subscription_token",
            "subscription token (CLAUDE_CODE_OAUTH_TOKEN)",
            permitted=False,
            detail="claude.ai subscription credentials are not permitted for third-party Agent SDK apps",
        )

    profile = _profile_name()
    profile_type = _profile_auth_type(profile) if profile else ""
    federated = profile_type == "oidc_federation" or all(_set(v) for v in FEDERATION_VARS)
    if federated:
        via = f"profile '{profile}'" if profile_type == "oidc_federation" else "environment"
        return AuthStatus("federation", f"Workload Identity Federation ({via})", permitted=True)

    # A user_oauth active profile ranks BELOW a working /login; a named one
    # (ANTHROPIC_PROFILE) ranks above. Mirror that.
    named = bool(os.environ.get("ANTHROPIC_PROFILE"))
    if profile and named:
        return AuthStatus("console_profile", f"Console sign-in (profile '{profile}')", permitted=True)
    login = _stored_login()
    if login is not None:
        return login
    if profile:
        return AuthStatus("console_profile", f"Console sign-in (profile '{profile}')", permitted=True)

    if sys.platform == "darwin":
        return AuthStatus(
            None,
            "undetermined",
            permitted=True,
            detail="no env var or file found; a Claude Code login may live in the macOS Keychain",
        )
    return AuthStatus(None, "none", permitted=False, detail="no credential found")
