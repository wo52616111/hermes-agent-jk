"""Shared, cross-process subscription-quota state (spec eva-018, design §1)."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agent import quota_state as qs
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def qdir(tmp_path, monkeypatch):
    monkeypatch.setattr(qs, "quota_dir", lambda: tmp_path / "quota")
    monkeypatch.setattr(qs, "_utc_now", lambda: NOW)
    monkeypatch.setattr(qs, "default_connected", lambda provider: True)
    monkeypatch.setattr(qs, "default_multi_account", lambda provider: False)
    monkeypatch.setattr(qs, "default_fetcher", lambda provider: pytest.fail("real fetch"))
    monkeypatch.setattr(qs, "credential_files", lambda: [])
    qs._last_touch.clear()
    qs._connected_cache.clear()
    return tmp_path / "quota"


def _snap(provider, used5=10.0, used7=40.0):
    return AccountUsageSnapshot(
        provider=provider, source="t", fetched_at=NOW,
        windows=(AccountUsageWindow("Current session", used5, NOW + timedelta(hours=3)),
                 AccountUsageWindow("Current week", used7, NOW + timedelta(days=3))))


class _Fetcher:
    def __init__(self, result=None, error=None):
        self.calls, self.result, self.error = 0, result, error

    def __call__(self, provider):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result or _snap(provider)


def _http_error(status, retry_after=None):
    req = httpx.Request("GET", "https://example.test/usage")
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(status, headers=headers, request=req))


def test_bootstrap_fetches_once_then_only_when_dirty(qdir):
    f = _Fetcher()
    assert qs.refresh("anthropic", fetcher=f) == "fetched"
    assert qs.refresh("anthropic", fetcher=f) == "fresh"
    assert f.calls == 1
    qs.mark_called("anthropic", now=NOW + timedelta(seconds=30))
    assert qs.refresh("anthropic", fetcher=f, now=NOW + timedelta(seconds=40)) == "floor"
    assert qs.refresh("anthropic", fetcher=f, now=NOW + timedelta(seconds=90)) == "fetched"
    assert f.calls == 2


def test_uncalled_provider_is_never_refetched(qdir):
    f = _Fetcher()
    qs.refresh("openai-codex", fetcher=f)
    for minutes in (5, 60, 600):
        assert qs.refresh("openai-codex", fetcher=f, now=NOW + timedelta(minutes=minutes)) == "fresh"
    assert f.calls == 1


def test_snapshot_written_with_periods_and_window_seconds(qdir):
    qs.refresh("anthropic", fetcher=_Fetcher())
    snap = qs.read_snapshot("anthropic")
    periods = {w["period"]: w for w in snap["windows"]}
    assert periods["5h"]["used_percent"] == 10.0
    assert periods["7d"]["window_seconds"] == 7 * 86400
    assert snap["provider"] == "anthropic"
    hist = (qdir / "history.jsonl").read_text().splitlines()
    assert len(hist) == 1


def test_429_sets_backoff_from_retry_after_and_keeps_last_windows(qdir):
    qs.refresh("anthropic", fetcher=_Fetcher())
    qs.mark_called("anthropic", now=NOW + timedelta(minutes=2))
    bad = _Fetcher(error=qs.QuotaFetchError("anthropic", 429, 260, "rate limited"))
    t1 = NOW + timedelta(minutes=3)
    assert qs.refresh("anthropic", fetcher=bad, now=t1) == "error"
    snap = qs.read_snapshot("anthropic")
    assert snap["windows"] and snap["error"]["status"] == 429
    assert qs._dt(snap["backoff_until"]) == t1 + timedelta(seconds=260)
    assert qs.refresh("anthropic", fetcher=bad, now=t1 + timedelta(seconds=200)) == "backoff"
    assert bad.calls == 1
    good = _Fetcher()
    assert qs.refresh("anthropic", fetcher=good, now=t1 + timedelta(seconds=261)) == "fetched"
    assert qs.read_snapshot("anthropic")["error"] is None


def test_non_429_error_backs_off_exponentially(qdir):
    qs.mark_called("openai-codex", now=NOW)
    bad = _Fetcher(error=qs.QuotaFetchError("openai-codex", 500, None, "boom"))
    qs.refresh("openai-codex", fetcher=bad, now=NOW)
    first = qs._dt(qs.read_snapshot("openai-codex")["backoff_until"]) - NOW
    qs.refresh("openai-codex", fetcher=bad, now=NOW + first + timedelta(seconds=1))
    second = qs._dt(qs.read_snapshot("openai-codex")["backoff_until"]) - (NOW + first + timedelta(seconds=1))
    assert first == timedelta(seconds=60)
    assert second == timedelta(seconds=120)


def test_retry_after_cap(qdir):
    qs.mark_called("anthropic", now=NOW)
    qs.refresh("anthropic", fetcher=_Fetcher(error=qs.QuotaFetchError("anthropic", 429, 99999, "x")), now=NOW)
    assert qs._dt(qs.read_snapshot("anthropic")["backoff_until"]) == NOW + timedelta(hours=1)


def test_error_from_http_status(qdir):
    err = qs.QuotaFetchError.from_exception("anthropic", _http_error(429, 120))
    assert (err.status, err.retry_after_s) == (429, 120)
    err = qs.QuotaFetchError.from_exception("anthropic", httpx.ConnectError("eof"))
    assert err.status == 0


def test_not_connected_never_fetches(qdir):
    f = _Fetcher()
    assert qs.refresh("anthropic", fetcher=f, connected=lambda p: False) == "not_connected"
    assert f.calls == 0 and qs.read_snapshot("anthropic") is None


def test_proxy_provider_is_refused(qdir):
    f = _Fetcher()
    assert qs.refresh("aftership-codex-proxy", fetcher=f) == "unsupported"
    qs.mark_called("aftership-codex-proxy")
    assert not (qdir / "dirty").exists() or not list((qdir / "dirty").iterdir())
    assert f.calls == 0


def test_mark_called_is_throttled(qdir, monkeypatch):
    touches = []
    monkeypatch.setattr(qs, "_touch", lambda path: touches.append(path))
    qs.mark_called("anthropic", now=NOW)
    qs.mark_called("anthropic", now=NOW + timedelta(seconds=10))
    qs.mark_called("anthropic", now=NOW + timedelta(seconds=31))
    assert len(touches) == 2


def test_reset_rollover_on_read(qdir):
    qs.refresh("anthropic", fetcher=_Fetcher(result=_snap("anthropic", used5=90, used7=95)))
    later = NOW + timedelta(days=3, hours=1)
    snap = qs.read_snapshot("anthropic", now=later)
    w7 = next(w for w in snap["windows"] if w["period"] == "7d")
    assert w7["used_percent"] == 0 and w7["rolled"] is True
    assert qs._dt(w7["reset_at"]) > later


def test_concurrent_refresh_fetches_once(qdir):
    import threading
    gate = threading.Event()
    calls = []

    def slow(provider):
        calls.append(1)
        gate.wait(2)
        return _snap(provider)

    t = threading.Thread(target=qs.refresh, args=("anthropic",), kwargs={"fetcher": slow})
    t.start()
    import time
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls
    # Second refresher waits on the lock, then sees a fresh snapshot.
    r2 = []
    t2 = threading.Thread(target=lambda: r2.append(qs.refresh("anthropic", fetcher=slow)))
    t2.start()
    gate.set()
    t.join(); t2.join()
    assert len(calls) == 1 and r2 == ["fresh"]


def test_read_all_lists_connected_snapshots(qdir):
    qs.refresh("anthropic", fetcher=_Fetcher())
    qs.refresh("openai-codex", fetcher=_Fetcher())
    assert {s["provider"] for s in qs.read_all()} == {"anthropic", "openai-codex"}


def test_connected_check_only_when_fetching(qdir):
    qs.refresh("anthropic", fetcher=_Fetcher())
    seen = []
    assert qs.refresh("anthropic", fetcher=_Fetcher(), connected=lambda p: seen.append(p) or True,
                      now=NOW + timedelta(minutes=5)) == "fresh"
    assert seen == []


def test_stale_refresh_fetches_uncalled_provider(qdir):
    f = _Fetcher()
    qs.refresh("openai-codex", fetcher=f)
    later = NOW + timedelta(minutes=45)
    assert qs.refresh("openai-codex", fetcher=f, now=later) == "fresh"
    assert qs.refresh("openai-codex", fetcher=f, now=later, stale_s=1800) == "fetched"
    assert f.calls == 2
    # Still respects backoff.
    qs.refresh("openai-codex", fetcher=_Fetcher(error=qs.QuotaFetchError("openai-codex", 429, 600, "x")),
               now=later + timedelta(minutes=40), stale_s=1800)
    assert qs.refresh("openai-codex", fetcher=f, now=later + timedelta(minutes=45), stale_s=1800) == "backoff"


def test_opencode_go_base_url_maps_to_go(qdir):
    qs.mark_called("opencode-go-bridge", base_url="https://opencode.ai/zen/go/v1", now=NOW)
    assert (qdir / "dirty" / "opencode-go").exists()


def test_main_loop_usage_marks_dirty(qdir):
    from types import SimpleNamespace
    from agent import turn_usage
    agent = SimpleNamespace(provider="openai-codex", base_url="", session_api_calls=0)
    turn_usage._mark_quota_dirty(agent)
    assert (qdir / "dirty" / "openai-codex").exists()


def test_aux_usage_without_usage_still_marks_dirty(qdir):
    from types import SimpleNamespace
    from agent.aux_accounting import record_aux_usage
    record_aux_usage(SimpleNamespace(usage=None), "title_generation", provider="anthropic")
    assert (qdir / "dirty" / "anthropic").exists()


def test_model_usage_writer_marks_dirty(qdir, tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s1", source="cli", model="gpt-6-sol")
        db.record_auxiliary_usage("s1", "background_review", model="gpt-6-sol",
                                  billing_provider="openai-codex", input_tokens=5)
        db.flush() if hasattr(db, "flush") else None
    finally:
        db.close()
    assert (qdir / "dirty" / "openai-codex").exists()


def test_anthropic_usage_403_retries_with_profile_scoped_token(monkeypatch):
    """A setup-token (inference-only scope) gets 403 from /api/oauth/usage; the fetcher retries once
    with the pooled OAuth login, never with the same token."""
    import agent.account_usage as au

    monkeypatch.setattr(au, "resolve_anthropic_token", lambda: "sk-ant-oat01-setup")
    monkeypatch.setattr(au, "_is_oauth_token", lambda t: True)
    monkeypatch.setattr(au, "_anthropic_usage_fallback_tokens", lambda exclude: ["sk-ant-oat01-pool"])
    seen = []

    def fake_get(url, headers, timeout):
        seen.append(headers["Authorization"])
        if headers["Authorization"].endswith("setup"):
            raise _http_error(403)
        return {"five_hour": {"utilization": 44.0, "resets_at": "2026-10-05T06:20:00+00:00"},
                "seven_day": {"utilization": 71.0, "resets_at": "2026-10-07T15:00:00+00:00"}}

    monkeypatch.setattr(au, "_get_json", fake_get)
    snap = au._fetch_anthropic_account_usage()
    assert [w.used_percent for w in snap.windows] == [44.0, 71.0]
    assert seen == ["Bearer sk-ant-oat01-setup", "Bearer sk-ant-oat01-pool"]


def test_anthropic_usage_fallback_uses_rate_limited_login_and_skips_dead(monkeypatch):
    """Inference rate limiting (pool status `exhausted`) does not stop a usage read, so such a login is a
    fallback candidate; `dead` logins are not. Candidates are tried in order past a 401."""
    import agent.account_usage as au
    from types import SimpleNamespace
    import agent.credential_pool as cp

    entries = [
        SimpleNamespace(access_token="dead-login", refresh_token="r", last_status="dead", auth_type="oauth",
                        source="manual"),
        SimpleNamespace(access_token="limited-login", refresh_token="r", last_status="exhausted",
                        auth_type="oauth", source="manual"),
        SimpleNamespace(access_token="setup", refresh_token=None, last_status="ok", auth_type="oauth",
                        source="env:ANTHROPIC_TOKEN"),
    ]
    monkeypatch.setattr(cp, "load_pool", lambda provider: SimpleNamespace(entries=lambda: entries))
    monkeypatch.setattr(au, "_claude_code_access_token", lambda: "stale-cc")
    monkeypatch.setattr(au, "_is_oauth_token", lambda t: True)
    assert au._anthropic_usage_fallback_tokens(exclude="setup") == ["limited-login", "stale-cc"]

    monkeypatch.setattr(au, "resolve_anthropic_token", lambda: "setup")
    monkeypatch.setattr(au, "_anthropic_usage_fallback_tokens", lambda exclude: ["revoked", "good"])
    seen = []

    def fake_get(url, headers, timeout):
        tok = headers["Authorization"].split()[-1]
        seen.append(tok)
        if tok == "setup":
            raise _http_error(403)
        if tok == "revoked":
            raise _http_error(401)
        return {"five_hour": {"utilization": 10.0, "resets_at": "2026-10-05T06:20:00+00:00"}}

    monkeypatch.setattr(au, "_get_json", fake_get)
    assert [w.used_percent for w in au._fetch_anthropic_account_usage().windows] == [10.0]
    assert seen == ["setup", "revoked", "good"]


def test_anthropic_usage_429_is_not_retried_with_other_token(monkeypatch):
    import agent.account_usage as au

    monkeypatch.setattr(au, "resolve_anthropic_token", lambda: "sk-ant-oat01-setup")
    monkeypatch.setattr(au, "_is_oauth_token", lambda t: True)
    monkeypatch.setattr(au, "_anthropic_usage_fallback_tokens", lambda exclude: pytest.fail("no retry on 429"))
    monkeypatch.setattr(au, "_get_json", lambda *a, **k: (_ for _ in ()).throw(_http_error(429, 260)))
    with pytest.raises(httpx.HTTPStatusError):
        au._fetch_anthropic_account_usage()


def test_call_after_a_fetch_is_not_lost_to_the_dirty_throttle(qdir):
    f = _Fetcher()
    qs.mark_called("anthropic", now=NOW - timedelta(seconds=10))
    assert qs.refresh("anthropic", fetcher=f, now=NOW + timedelta(seconds=1)) == "fetched"
    qs.mark_called("anthropic", now=NOW + timedelta(seconds=5))
    assert qs.refresh("anthropic", fetcher=f, now=NOW + timedelta(seconds=62)) == "fetched"
    assert f.calls == 2


def test_status_groups_hide_disconnected_providers(qdir, monkeypatch):
    qs.refresh("anthropic", fetcher=_Fetcher())
    qs.refresh("openai-codex", fetcher=_Fetcher())
    groups = qs.status_groups(connected=lambda p: p == "openai-codex")
    assert [g["provider"] for g in groups] == ["openai-codex"]


def test_status_groups_cache_the_connected_check(qdir, monkeypatch):
    qs.refresh("anthropic", fetcher=_Fetcher())
    calls = []
    monkeypatch.setattr(qs, "default_connected", lambda p: calls.append(p) or True)
    qs._connected_cache.clear()
    qs.status_groups()
    qs.status_groups()
    assert calls.count("anthropic") == 1


def test_multi_account_pool_reports_unknown_quota(qdir, monkeypatch):
    qs.refresh("anthropic", fetcher=_Fetcher())
    monkeypatch.setattr(qs, "default_multi_account", lambda p: p == "anthropic")
    qs._connected_cache.clear()
    groups = {g["provider"]: g for g in qs.status_groups()}
    assert groups["anthropic"]["windows"] == []
    assert groups["anthropic"]["multi_account"] is True
    assert groups["anthropic"]["error"] == {"status": 0, "message": "multi_account"}

    f = _Fetcher()
    qs.mark_called("anthropic", now=NOW + timedelta(minutes=2))
    assert qs.refresh("anthropic", fetcher=f, now=NOW + timedelta(minutes=3)) == "multi_account"
    assert f.calls == 0
    stored = qs.read_snapshot("anthropic")
    assert stored["windows"] == [] and stored["multi_account"] is True


def test_cli_json_marks_multi_account_as_unknown(qdir, monkeypatch, capsys):
    import argparse
    import json
    from hermes_cli.subcommands.usage import cmd_usage

    qs.refresh("anthropic", fetcher=_Fetcher())
    qs.refresh("openai-codex", fetcher=_Fetcher())
    monkeypatch.setattr(qs, "default_multi_account", lambda p: p == "anthropic")
    qs._connected_cache.clear()
    cmd_usage(argparse.Namespace(subscriptions=True, refresh=False, json=True, providers=[]))
    subs = {g["provider"]: g for g in json.loads(capsys.readouterr().out)["subscriptions"]}
    assert subs["anthropic"]["windows"] == [] and subs["anthropic"]["multi_account"] is True
    assert subs["openai-codex"]["windows"] and not subs["openai-codex"].get("multi_account")


def test_connected_cache_follows_credential_file_changes(qdir, monkeypatch, tmp_path):
    import os
    qs.refresh("anthropic", fetcher=_Fetcher())
    cred = tmp_path / "auth.json"
    cred.write_text("{}")
    monkeypatch.setattr(qs, "credential_files", lambda: [cred])
    state = {"connected": True}
    monkeypatch.setattr(qs, "default_connected", lambda p: state["connected"])
    qs._connected_cache.clear()
    assert [g["provider"] for g in qs.status_groups()] == ["anthropic"]

    state["connected"] = False
    later = cred.stat().st_mtime + 5
    os.utime(cred, (later, later))
    assert qs.status_groups(now=NOW + timedelta(seconds=10)) == []


def test_connected_cache_still_expires_without_file_changes(qdir, monkeypatch):
    qs.refresh("anthropic", fetcher=_Fetcher())
    state = {"connected": True}
    monkeypatch.setattr(qs, "default_connected", lambda p: state["connected"])
    qs._connected_cache.clear()
    assert qs.status_groups()
    state["connected"] = False
    assert qs.status_groups(now=NOW + timedelta(seconds=10))
    assert qs.status_groups(now=NOW + timedelta(seconds=qs.CONNECTED_CACHE_S + 1)) == []
