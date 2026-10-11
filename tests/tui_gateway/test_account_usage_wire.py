"""The gateway wire carries each quota account's own windows (spec D19/D26/D41).

Behaviour contracts for ``_account_usage_all_wire`` (per-account ``accounts[]`` + ``serving``,
never a blended number) and the session-local active account on ``_session_usage_snapshot``.
Real imports, a temp quota dir, no network. Regression for the live ``go ·err`` footer: a
multi-account group must carry a real per-account view instead of the ``{status: 0}`` marker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent import quota_state as qs

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
PROVIDER = "opencode-go"


@pytest.fixture
def qdir(tmp_path, monkeypatch):
    """Temp quota dir; no network, no real config, no real credential access."""
    root = tmp_path / "quota"
    monkeypatch.setattr(qs, "quota_dir", lambda: root)
    monkeypatch.setattr(qs, "default_fetcher", lambda provider: pytest.fail("real fetch"))
    monkeypatch.setattr(qs, "default_connected", lambda provider: True)
    monkeypatch.setattr(qs, "default_multi_account", lambda provider: False)
    monkeypatch.setattr(qs, "credential_files", lambda: [])
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)
    monkeypatch.setattr(qs, "_provider_profile", lambda provider: SimpleNamespace(bills_when_plan_spent=True))
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    qs._connected_cache.clear()
    yield root


def _window(period="7d", percent=0.0, reset_days=3, status=None):
    return {
        "period": period,
        "used_percent": percent,
        "status": status,
        "reset_at": (NOW + timedelta(days=reset_days)).isoformat(),
        "window_seconds": 7 * 86400,
    }


def _store(fp, label, windows, *, error=None):
    qs.store_account_facts(
        PROVIDER, fp, windows,
        entries=[{"profile": "default", "entry_id": fp[-3:], "label": label}],
        fetched_at=NOW.isoformat(), attempted_at=NOW.isoformat(), error=error, now=NOW,
    )


def _groups_by_provider(monkeypatch, groups):
    monkeypatch.setattr(qs, "status_groups", lambda *a, **k: list(groups))
    monkeypatch.setattr(qs, "current_profile", lambda: "default")
    from tui_gateway import server

    return {g["provider"]: g for g in server._account_usage_all_wire()}


def test_account_usage_all_wire_replaces_a_multi_account_group_with_per_account_rows(qdir, monkeypatch):
    """A quota_aware provider's group carries every account's own windows (D41), with no ``err``
    marker (D19)."""
    _store("sha256:bbb", "OPENCODE_GO_API_KEY_2", [_window(period="5h", percent=1.0), _window(percent=0.0)])
    _store("sha256:aaa", "OPENCODE_GO_API_KEY", [_window(percent=100.0), _window(period="monthly", percent=86.0)])
    legacy = [
        {"provider": PROVIDER, "fetched_at": None, "age_s": None,
         "error": {"status": 0, "message": "multi_account"}, "backoff_until": None,
         "windows": [], "hint": None, "multi_account": True},
        {"provider": "anthropic", "fetched_at": "x", "age_s": 12, "error": None, "backoff_until": None,
         "windows": [{"period": "7d", "used_percent": 10.0, "reset_at": None}], "hint": None},
    ]
    groups = _groups_by_provider(monkeypatch, legacy)

    go = groups[PROVIDER]
    # A multi-account group carries no error marker of its own.
    assert go["error"] is None
    assert go["multi_account"] is True
    assert len(go["accounts"]) == 2
    states = {a["label"]: a["state"] for a in go["accounts"]}
    assert states == {"OPENCODE_GO_API_KEY_2": "covered", "OPENCODE_GO_API_KEY": "overflow"}
    assert all(a["read_health"] in {"ok", "stale"} for a in go["accounts"])
    assert go["serving"] in {"sha256:bbb", "sha256:aaa"}
    # The legacy provider's group is byte-identical (the wire change is additive).
    assert groups["anthropic"] == legacy[1]


def test_account_usage_all_wire_never_blends_accounts(qdir, monkeypatch):
    """The provider line's windows are the SERVING account's own - never a sum or an average."""
    _store("sha256:bbb", "B", [_window(period="5h", percent=1.0)])
    _store("sha256:aaa", "A", [_window(percent=100.0), _window(period="monthly", percent=86.0)])
    groups = _groups_by_provider(monkeypatch, [])
    go = groups[PROVIDER]

    serving = next(a for a in go["accounts"] if a["fingerprint"] == go["serving"])
    assert go["windows"] == serving["windows"]
    assert len(go["windows"]) == len(serving["windows"])


def test_account_wire_surfaces_a_429_read_status(qdir, monkeypatch):
    """A rate-limited read keeps its effective state and reports the HTTP status the UI marks."""
    _store("sha256:aaa", "A", [_window(percent=10.0)],
           error={"status": 429, "message": "rate limited", "backoff_s": 300})
    groups = _groups_by_provider(monkeypatch, [])
    account = groups[PROVIDER]["accounts"][0]

    assert account["read_health"] == "error"
    assert account["error_status"] == 429
    assert account["state"] == "covered"  # an unreadable account is never `unknown`


def test_non_quota_aware_provider_group_is_untouched(qdir, monkeypatch):
    """A provider with no view keeps today's group exactly (including the multi_account marker)."""
    legacy = {"provider": "anthropic", "fetched_at": None, "age_s": None,
              "error": {"status": 0, "message": "multi_account"}, "backoff_until": None,
              "windows": [], "hint": None, "multi_account": True}
    groups = _groups_by_provider(monkeypatch, [legacy])

    assert groups["anthropic"] == legacy
    assert "accounts" not in groups["anthropic"]


def test_session_usage_snapshot_marks_the_session_active_account(qdir, monkeypatch):
    """``_session_usage_snapshot`` adds the session-local active account (D19/D29) alongside the
    shared per-account view - two sessions on different accounts do not share one flag."""
    _store("sha256:bbb", "B", [_window(percent=0.0)])
    _store("sha256:aaa", "A", [_window(percent=100.0)])
    monkeypatch.setattr(qs, "status_groups", lambda *a, **k: [])
    monkeypatch.setattr(qs, "current_profile", lambda: "default")
    monkeypatch.setattr(qs, "served_fingerprint", lambda provider, **k: "sha256:aaa")
    from tui_gateway import server

    agent = SimpleNamespace(provider=PROVIDER, base_url="https://opencode.ai/zen/go/v1",
                            api_key="tok", model="m")
    usage = server._session_usage_snapshot({"agent": agent, "history": [], "running": False})

    assert usage["account_usage_active"] == PROVIDER
    assert usage["account_usage_active_account"] == "sha256:aaa"
    go = next(g for g in usage["account_usage_all"] if g["provider"] == PROVIDER)
    assert len(go["accounts"]) == 2
