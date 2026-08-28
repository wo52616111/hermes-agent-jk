from datetime import datetime, timezone
from types import SimpleNamespace

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from tui_gateway import server


def _agent(provider="openai-codex"):
    return SimpleNamespace(
        provider=provider,
        model="gpt-5.6-sol",
        session_input_tokens=10,
        session_output_tokens=5,
        session_total_tokens=15,
        session_api_calls=1,
        context_compressor=None,
    )


def test_session_usage_includes_cached_capacity_windows_with_reset_times():
    reset_5h = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)
    reset_7d = datetime(2026, 9, 2, 8, 30, tzinfo=timezone.utc)
    snapshot = AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
        windows=(
            AccountUsageWindow(label="Session", used_percent=34, reset_at=reset_5h),
            AccountUsageWindow(label="Weekly", used_percent=62, reset_at=reset_7d),
        ),
    )
    session = {"agent": _agent(), "_account_usage_snapshot": snapshot}

    usage = server._session_usage_snapshot(session)

    assert usage["account_usage"] == {
        "provider": "openai-codex",
        "fetched_at": "2026-08-28T10:00:00+00:00",
        "windows": [
            {"period": "5h", "used_percent": 34.0, "reset_at": "2026-08-28T15:00:00+00:00"},
            {"period": "7d", "used_percent": 62.0, "reset_at": "2026-09-02T08:30:00+00:00"},
        ],
    }


def test_session_usage_matches_provider_case_insensitively():
    snapshot = AccountUsageSnapshot(
        provider="anthropic",
        source="oauth_usage_api",
        fetched_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
        windows=(AccountUsageWindow(label="Current session", used_percent=19),),
    )
    session = {"agent": _agent("Anthropic"), "_account_usage_snapshot": snapshot}

    usage = server._session_usage_snapshot(session)

    assert usage["account_usage"]["provider"] == "anthropic"
    assert usage["account_usage"]["windows"][0]["period"] == "5h"


def test_session_usage_clears_quota_from_previous_provider():
    snapshot = AccountUsageSnapshot(
        provider="anthropic",
        source="oauth_usage_api",
        fetched_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
        windows=(AccountUsageWindow(label="Current session", used_percent=10),),
    )
    session = {"agent": _agent("openai-codex"), "_account_usage_snapshot": snapshot}

    usage = server._session_usage_snapshot(session)

    assert usage["account_usage"] is None


def test_account_usage_refresh_fetches_in_background_and_pushes_status(monkeypatch):
    snapshot = AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
        windows=(AccountUsageWindow(label="Session", used_percent=34),),
    )
    agent = _agent()
    agent.base_url = "https://chatgpt.com/backend-api/codex"
    agent.api_key = "token"
    session = {"agent": agent}
    emitted = []

    monkeypatch.setattr("agent.account_usage.fetch_account_usage", lambda *a, **kw: snapshot)
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))

    thread = server._refresh_account_usage_async("sid", session)
    thread.join(timeout=1)

    assert session["_account_usage_snapshot"] is snapshot
    assert emitted[-1][0:2] == ("session.usage", "sid")
    assert emitted[-1][2]["usage"]["account_usage"]["windows"][0]["period"] == "5h"


def test_account_usage_refresh_coalesces_turn_while_request_is_running(monkeypatch):
    import threading

    first_started = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    calls = 0
    snapshot = AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
        windows=(AccountUsageWindow(label="Session", used_percent=34),),
    )

    def fetch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            release_first.wait(timeout=1)
        else:
            second_done.set()
        return snapshot

    session = {"agent": _agent()}
    monkeypatch.setattr("agent.account_usage.fetch_account_usage", fetch)
    monkeypatch.setattr(server, "_emit", lambda *_args: None)

    first = server._refresh_account_usage_async("sid", session)
    assert first_started.wait(timeout=1)
    server._refresh_account_usage_async("sid", session)
    release_first.set()

    assert second_done.wait(timeout=1)
    assert calls == 2


def test_account_usage_refresh_claim_is_atomic(monkeypatch):
    import threading
    import time

    class SlowSession(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            if key == "_account_usage_refreshing" and not value:
                time.sleep(0.02)
            return value

    active = 0
    max_active = 0
    active_lock = threading.Lock()
    release = threading.Event()
    snapshot = AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
        windows=(AccountUsageWindow(label="Session", used_percent=34),),
    )

    def fetch(*_args, **_kwargs):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        release.wait(timeout=1)
        with active_lock:
            active -= 1
        return snapshot

    session = SlowSession(agent=_agent())
    monkeypatch.setattr("agent.account_usage.fetch_account_usage", fetch)
    monkeypatch.setattr(server, "_emit", lambda *_args: None)

    callers = [threading.Thread(target=server._refresh_account_usage_async, args=("sid", session)) for _ in range(8)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=1)
    time.sleep(0.05)
    release.set()

    assert max_active == 1


def test_account_usage_refresh_failure_keeps_last_success(monkeypatch):
    previous = AccountUsageSnapshot(
        provider="anthropic",
        source="oauth_usage_api",
        fetched_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
        windows=(AccountUsageWindow(label="Current week", used_percent=20),),
    )
    session = {"agent": _agent("anthropic"), "_account_usage_snapshot": previous}

    def fail(*_args, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("agent.account_usage.fetch_account_usage", fail)
    monkeypatch.setattr(server, "_emit", lambda *_args: None)

    thread = server._refresh_account_usage_async("sid", session)
    thread.join(timeout=1)

    assert session["_account_usage_snapshot"] is previous
