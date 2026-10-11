"""B3: ``hermes usage --all-subscriptions`` gains a nested per-account array (spec D20).

Additive only: the ``subscriptions`` array's existing keys and semantics are untouched; a group with
a per-account view gains ``accounts[]`` (effective ``state`` + ``read_health``, short fingerprint,
never key material). A provider without a view keeps today's group byte-for-byte.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent import quota_state as qs

NOW = datetime.now(timezone.utc)
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
    monkeypatch.setattr(qs, "current_profile", lambda: "default")
    qs._connected_cache.clear()
    yield root


def _window(period="7d", percent=0.0):
    return {
        "period": period,
        "used_percent": percent,
        "status": None,
        "reset_at": (NOW + timedelta(days=3)).isoformat(),
        "window_seconds": 7 * 86400,
    }


def _store(fp, label, windows):
    qs.store_account_facts(
        PROVIDER, fp, windows,
        entries=[{"profile": "default", "entry_id": fp[-3:], "label": label}],
        fetched_at=NOW.isoformat(), attempted_at=NOW.isoformat(), error=None, now=NOW,
    )


def _args(**kw):
    return argparse.Namespace(**{"subscriptions": True, "refresh": False, "json": True, "providers": [], **kw})


def _legacy_group():
    return {
        "provider": PROVIDER, "fetched_at": NOW.isoformat(), "age_s": 300, "error": None,
        "backoff_until": None, "windows": [{"period": "7d", "used_percent": 0.0, "reset_at": None}],
        "hint": None,
    }


def _run(monkeypatch, group, args):
    monkeypatch.setattr(qs, "status_groups", lambda *a, **k: [group])
    from hermes_cli.subcommands.usage import cmd_usage

    return cmd_usage(args)


def test_all_subscriptions_json_adds_nested_accounts(qdir, monkeypatch, capsys):
    _store("sha256:bbb444555666", "OPENCODE_GO_API_KEY_2", [_window(period="5h", percent=1.0)])
    _store("sha256:aaa111222333", "OPENCODE_GO_API_KEY", [_window(percent=100.0)])
    group = _legacy_group()
    original_windows = list(group["windows"])

    assert _run(monkeypatch, group, _args()) == 0
    doc = json.loads(capsys.readouterr().out)

    subs = {g["provider"]: g for g in doc["subscriptions"]}
    # Additive: the group's existing keys and values are unchanged.
    assert subs[PROVIDER]["windows"] == original_windows
    assert subs[PROVIDER]["age_s"] == 300
    by_label = {a["label"]: a for a in subs[PROVIDER]["accounts"]}
    assert set(by_label) == {"OPENCODE_GO_API_KEY_2", "OPENCODE_GO_API_KEY"}
    assert by_label["OPENCODE_GO_API_KEY_2"]["state"] == "covered"
    assert by_label["OPENCODE_GO_API_KEY"]["state"] == "overflow"
    assert by_label["OPENCODE_GO_API_KEY"]["metered"] is True
    assert all(a["read_health"] for a in subs[PROVIDER]["accounts"])
    assert all(a["windows"] for a in subs[PROVIDER]["accounts"])
    # Short fingerprint only - no full digest, no key material.
    assert all(len(a["fingerprint"]) == 6 for a in subs[PROVIDER]["accounts"])
    assert "sha256" not in json.dumps(doc)


def test_all_subscriptions_leaves_a_provider_without_a_view_untouched(qdir, monkeypatch, capsys):
    group = {"provider": "anthropic", "fetched_at": None, "age_s": None,
             "error": {"status": 0, "message": "multi_account"}, "backoff_until": None,
             "windows": [], "hint": None, "multi_account": True}

    assert _run(monkeypatch, group, _args()) == 0
    doc = json.loads(capsys.readouterr().out)

    sub = doc["subscriptions"][0]
    assert sub["multi_account"] is True and sub["windows"] == []
    assert "accounts" not in sub


def test_all_subscriptions_human_output_lists_each_account(qdir, monkeypatch, capsys):
    _store("sha256:bbb444555666", "OPENCODE_GO_API_KEY_2", [_window(period="5h", percent=1.0)])
    _store("sha256:aaa111222333", "OPENCODE_GO_API_KEY", [_window(percent=100.0)])

    assert _run(monkeypatch, _legacy_group(), _args(json=False)) == 0
    out = capsys.readouterr().out

    assert "OPENCODE_GO_API_KEY_2 [bbb444]" in out
    assert "overflow(metered)" in out
    assert "sha256" not in out


def test_all_subscriptions_reports_a_provider_with_only_per_account_facts(qdir, monkeypatch, capsys):
    """A ``quota_aware`` provider never serves a legacy single-account snapshot (B1/D41): the
    provider-level ``status_groups()`` entry, when present, carries the SERVING account's OWN view
    windows (never the other account's, never a stale snapshot) and is absent when the view has none.
    The CLI must still list the provider and its nested per-account records - built from its view -
    instead of printing an empty ``subscriptions`` list while the gateway renders the same accounts.
    This does NOT mock ``status_groups``: it exercises the real integration path."""
    _store("sha256:bbb444555666", "OPENCODE_GO_API_KEY_2", [_window(period="5h", percent=1.0)])
    _store("sha256:aaa111222333", "OPENCODE_GO_API_KEY", [_window(percent=100.0)])

    from hermes_cli.subcommands.usage import cmd_usage

    # D41: a ``quota_aware`` provider never serves a legacy single-account snapshot. Its provider-level
    # group, when present, carries the SERVING account's OWN view windows - never the other account's,
    # never a stale snapshot - and is absent entirely when the view has no windows.
    view = qs.read_view(qs.current_profile(), PROVIDER)
    assert view is not None
    serving_account = next(a for a in view["accounts"] if a["fingerprint"] == view["serving"])
    other_account = next(a for a in view["accounts"] if a["fingerprint"] != view["serving"])
    # The two accounts carry different periods, so "the serving account's own windows" is
    # distinguishable from "the other account's" - a group built from the wrong account cannot pass.
    assert [w["period"] for w in serving_account["windows"]] != [w["period"] for w in other_account["windows"]]
    expected_windows = [{k: w.get(k) for k in ("period", "used_percent", "reset_at", "rolled") if k in w}
                        for w in serving_account["windows"]]
    provider_groups = [g for g in qs.status_groups() if g["provider"] == PROVIDER]
    assert len(provider_groups) <= 1
    if provider_groups:
        assert provider_groups[0]["windows"] == expected_windows
    assert cmd_usage(_args()) == 0
    doc = json.loads(capsys.readouterr().out)

    subs = {g["provider"]: g for g in doc["subscriptions"]}
    assert PROVIDER in subs
    by_label = {a["label"]: a for a in subs[PROVIDER]["accounts"]}
    assert set(by_label) == {"OPENCODE_GO_API_KEY_2", "OPENCODE_GO_API_KEY"}
    assert by_label["OPENCODE_GO_API_KEY_2"]["state"] == "covered"
    assert by_label["OPENCODE_GO_API_KEY"]["state"] == "overflow"
    assert by_label["OPENCODE_GO_API_KEY"]["metered"] is True
    # The provider line carries the SERVING account's own windows, never a blend.
    assert {w["period"] for w in subs[PROVIDER]["windows"]} == {"5h"}
    assert "sha256" not in json.dumps(doc)
