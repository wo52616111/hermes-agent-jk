import threading
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


def test_session_usage_serializes_opencode_go_windows():
    snapshot = AccountUsageSnapshot(
        provider="opencode-go",
        source="usage_api",
        fetched_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
        windows=(
            AccountUsageWindow(label="5h", used_percent=12),
            AccountUsageWindow(label="7d", used_percent=34),
            AccountUsageWindow(label="monthly", used_percent=56),
        ),
    )
    agent = _agent("custom")
    agent.base_url = "https://opencode.ai/zen/go/v1/responses"
    session = {"agent": agent, "_account_usage_snapshot": snapshot}

    usage = server._session_usage_snapshot(session)

    assert [window["period"] for window in usage["account_usage"]["windows"]] == [
        "5h",
        "7d",
        "monthly",
    ]


def test_fetch_account_usage_supports_opencode_go(monkeypatch):
    """A custom entry pointed at the Go relay resolves the Go profile's usage hook.

    Upstream owns the fetch (``providers/model-providers/opencode-zen``): the Go profile's
    ``fetch_account_usage`` reads ``/zen/go/v1/usage``. What the downstream keeps is the
    bridge case — a custom provider name with an opencode.ai/zen/go base_url still resolves
    that hook, and the capacity row's wire map labels the windows 5h/7d/monthly.
    """

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "usage": {
                    "rolling": {"status": "ok", "percent": 12, "resetsAt": "2026-08-28T15:00:00+00:00"},
                    "weekly": {"status": "ok", "percent": 34, "resetsAt": "2026-09-02T08:30:00+00:00"},
                    "monthly": {"status": "ok", "percent": 56, "resetsAt": "2026-09-28T00:00:00+00:00"},
                }
            }

    class _Client:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None):
            return _Response()

    monkeypatch.setattr("httpx.Client", _Client)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda requested, explicit_base_url=None, explicit_api_key=None: {
            "provider": "opencode-go",
            "base_url": "https://opencode.ai/zen/go",
            "api_key": "sk-test",
        },
    )

    from agent.account_usage import fetch_account_usage

    snapshot = fetch_account_usage(
        "custom", base_url="https://opencode.ai/zen/go/v1/responses", api_key="secret"
    )

    assert snapshot is not None
    assert snapshot.provider == "opencode-go"
    assert [window.label for window in snapshot.windows] == ["Rolling window", "Weekly", "Monthly"]

    agent = _agent("custom")
    agent.base_url = "https://opencode.ai/zen/go/v1/responses"
    usage = server._session_usage_snapshot({"agent": agent, "_account_usage_snapshot": snapshot})

    assert [window["period"] for window in usage["account_usage"]["windows"]] == ["5h", "7d", "monthly"]

    # Without the Go relay in the base_url there is no bridge fallback to resolve.
    assert fetch_account_usage("custom", base_url="https://relay.example.com/v1", api_key="s") is None


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


def _isolated_quota(monkeypatch, tmp_path):
    from agent import quota_state
    monkeypatch.setattr(quota_state, "quota_dir", lambda: tmp_path / "quota")
    monkeypatch.setattr(quota_state, "default_connected", lambda provider: True)
    monkeypatch.setattr(quota_state, "default_multi_account", lambda provider: False)
    quota_state._last_touch.clear()
    quota_state._connected_cache.clear()
    return quota_state


def test_account_usage_refresh_uses_shared_state_and_pushes_status(monkeypatch, tmp_path):
    qs = _isolated_quota(monkeypatch, tmp_path)
    snapshot = AccountUsageSnapshot(
        provider="openai-codex", source="usage_api", fetched_at=datetime.now(timezone.utc),
        windows=(AccountUsageWindow(label="Session", used_percent=34),
                 AccountUsageWindow(label="Weekly", used_percent=7)),
    )
    fetched = []
    monkeypatch.setattr(qs, "default_fetcher", lambda p: fetched.append(p) or (snapshot if p == "openai-codex" else None))
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    session = {"agent": _agent()}

    thread = server._refresh_account_usage_async("sid", session)
    thread.join(timeout=2)

    # Bootstrap: every supported provider without a snapshot is fetched once.
    assert sorted(fetched) == ["anthropic", "openai-codex", "opencode-go"]
    usage = emitted[-1][2]["usage"]
    assert emitted[-1][0:2] == ("session.usage", "sid")
    groups = {g["provider"]: g for g in usage["account_usage_all"]}
    assert [w["period"] for w in groups["openai-codex"]["windows"]] == ["5h", "7d"]
    assert usage["account_usage"]["provider"] == "openai-codex"

    # Next turn: nothing was billed since, so nothing is fetched again.
    fetched.clear()
    server._refresh_account_usage_async("sid", session).join(timeout=2)
    assert fetched == []


def test_account_usage_refresh_only_fetches_billed_provider(monkeypatch, tmp_path):
    qs = _isolated_quota(monkeypatch, tmp_path)
    snap = lambda p: AccountUsageSnapshot(provider=p, source="t", fetched_at=datetime.now(timezone.utc),
                                          windows=(AccountUsageWindow(label="Weekly", used_percent=1),))
    fetched = []
    monkeypatch.setattr(qs, "default_fetcher", lambda p: fetched.append(p) or snap(p))
    monkeypatch.setattr(server, "_emit", lambda *_a: None)
    session = {"agent": _agent()}
    server._refresh_account_usage_async("sid", session).join(timeout=2)
    fetched.clear()

    from datetime import timedelta
    for p in qs.SUPPORTED:  # make the stored fetches older than the 60 s floor
        path = qs._path("snapshot", p, ".json")
        import json as _json
        data = _json.loads(path.read_text())
        old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        data["fetched_at"] = data["attempted_at"] = old
        path.write_text(_json.dumps(data))
    qs.mark_called("openai-codex")
    server._refresh_account_usage_async("sid", session).join(timeout=2)
    assert fetched == ["openai-codex"]


def test_account_usage_refresh_429_keeps_last_windows(monkeypatch, tmp_path):
    qs = _isolated_quota(monkeypatch, tmp_path)
    good = AccountUsageSnapshot(provider="anthropic", source="t", fetched_at=datetime.now(timezone.utc),
                                windows=(AccountUsageWindow(label="Current week", used_percent=20),))
    monkeypatch.setattr(qs, "default_fetcher", lambda p: good if p == "anthropic" else None)
    monkeypatch.setattr(server, "_emit", lambda *_a: None)
    session = {"agent": _agent("anthropic")}
    server._refresh_account_usage_async("sid", session).join(timeout=2)

    from datetime import timedelta
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    qs.mark_called("anthropic", now=later - timedelta(minutes=1))

    def fail(_p):
        raise qs.QuotaFetchError("anthropic", 429, 260, "rate limited")
    assert qs.refresh("anthropic", fetcher=fail, now=later) == "error"

    groups = {g["provider"]: g for g in server._account_usage_all_wire()}
    assert groups["anthropic"]["windows"][0]["used_percent"] == 20.0
    assert groups["anthropic"]["error"]["status"] == 429


def test_account_usage_refresh_coalesces_turn_while_request_is_running(monkeypatch, tmp_path):
    import threading

    qs = _isolated_quota(monkeypatch, tmp_path)
    started, release = threading.Event(), threading.Event()
    runs = []

    def slow_refresh(provider, **_kw):
        if provider == "anthropic":
            runs.append(1)
            if len(runs) == 1:
                started.set()
                release.wait(timeout=2)
        return "fresh"

    monkeypatch.setattr(qs, "refresh", slow_refresh)
    monkeypatch.setattr(server, "_emit", lambda *_a: None)
    session = {"agent": _agent()}
    first = server._refresh_account_usage_async("sid", session)
    assert started.wait(timeout=2)
    assert server._refresh_account_usage_async("sid", session) is None  # coalesced
    release.set()
    first.join(timeout=2)
    import time as _time
    deadline = _time.monotonic() + 2
    while len(runs) < 2 and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert len(runs) == 2


def test_account_usage_refresh_uses_the_session_profile_scope(monkeypatch, tmp_path):
    import contextlib

    qs = _isolated_quota(monkeypatch, tmp_path)
    active_profile = []
    refreshed = []

    @contextlib.contextmanager
    def scope(session):
        active_profile.append(session.get("profile_home"))
        try:
            yield
        finally:
            active_profile.pop()

    monkeypatch.setattr(server, "_session_profile_runtime_scope", scope)
    monkeypatch.setattr(qs, "refresh", lambda provider: refreshed.append((provider, active_profile[-1])) or "fresh")
    monkeypatch.setattr(server, "_emit", lambda *_a: None)

    thread = server._refresh_account_usage_async("sid", {"agent": None, "profile_home": "/profiles/secondary"})
    assert thread is not None
    thread.join(timeout=2)

    assert refreshed == [(provider, "/profiles/secondary") for provider in qs.SUPPORTED]


def test_completed_turn_schedules_provider_quota_refresh():
    """A finished TUI turn must schedule the quota refresh from the LIVE turn path.

    The hook once sat in server.py's copy of ``_run_prompt_submit``, which upstream's
    ``prompt_turn`` split shadows at import: the refresh then never ran and the capacity row
    silently kept only ``─ ctx …``, losing every provider window.
    """
    import contextlib
    import threading

    from tui_gateway import prompt_turn
    from tui_gateway.method_ctx import rebind

    refreshed, emitted = [], []
    agent = SimpleNamespace(session_id="agent-1", interim_assistant_callback=None)
    noop = lambda *_args, **_kwargs: None

    namespace = dict(vars(server))
    namespace.update({
        "_admit_prompt_turn": lambda *_a, **_k: ([], agent),
        "_prepare_turn_input": lambda *_a, **_k: ("prompt", "message", 80, None),
        "_invoke_agent": noop,
        "_absorb_turn_result": lambda *_a, **_k: "",
        "_complete_turn_payload": lambda *_a, **_k: ({}, "done", "complete"),
        "_emit": lambda event, *_a, **_k: emitted.append(event),
        "_refresh_account_usage_async": lambda sid, _session: refreshed.append(sid),
        "_goal_followup_after_turn": lambda *_a, **_k: None,
        "_after_complete_turn": noop,
        "_publish_session_control_snapshot": noop,
        "_finish_turn": noop,
        "_record_turn_marker": lambda *_a, **_k: "marker",
        "_retire_turn_marker": noop,
        "_clear_inflight_turn": noop,
        "_emit_settled_session_info": noop,
        "_run_post_turn_followups": noop,
        "_reopen_routed_session_row": noop,
        "_routing_provenance_db": lambda _session: contextlib.nullcontext(None),
        "_sessions": {},
        "bind_transport": noop,
        "reset_transport": noop,
    })

    submit = rebind(prompt_turn._run_prompt_submit, namespace)
    session = {
        "agent": agent, "history_lock": threading.RLock(), "running": False, "session_key": "key-1"}

    assert submit("rid", "sid-1", session, "hello") is True
    session["_run_thread"].join(timeout=10)

    assert refreshed == ["sid-1"]
    assert "message.complete" in emitted


def test_session_usage_names_the_active_quota_provider(monkeypatch):
    monkeypatch.setattr(server, "_account_usage_all_wire", lambda: [])
    usage = server._session_usage_snapshot({"agent": _agent("Anthropic")})
    assert usage["account_usage_active"] == "anthropic"

    go = SimpleNamespace(**{**vars(_agent("custom")), "base_url": "https://opencode.ai/zen/go/v1"})
    assert server._session_usage_snapshot({"agent": go})["account_usage_active"] == "opencode-go"


def test_gateway_startup_bootstraps_connected_quota_off_thread(monkeypatch):
    import threading
    from agent import quota_state
    from tui_gateway import entry

    ran = threading.Event()
    monkeypatch.setattr(quota_state, "bootstrap_connected", lambda *a, **k: ran.set() or {})
    thread = entry._start_quota_bootstrap()
    thread.join(5)
    assert ran.is_set() and thread.daemon


def test_quota_bootstrap_uses_the_requested_session_profile_scope(monkeypatch):
    import contextlib

    from agent import quota_state
    from tui_gateway import entry

    active_profile = []

    @contextlib.contextmanager
    def scope(session):
        active_profile.append(session.get("profile_home"))
        try:
            yield
        finally:
            active_profile.pop()

    seen = []
    monkeypatch.setattr(server, "_session_profile_runtime_scope", scope)
    monkeypatch.setattr(quota_state, "bootstrap_connected", lambda: seen.append(active_profile[-1] if active_profile else "unscoped"))
    monkeypatch.setattr(entry, "_publish_quota_usage", lambda **_kw: None)
    monkeypatch.setattr(server, "_sessions", {"sid": {"profile_home": "/profiles/secondary"}})

    thread = entry._start_quota_bootstrap()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert seen == ["/profiles/secondary"]


def test_quota_usage_tick_does_not_hold_refresh_lock_during_a_slow_emit(monkeypatch):
    import contextlib

    from tui_gateway import entry

    emit_started = threading.Event()
    release_emit = threading.Event()
    emitted = []
    session = {
        "agent": None,
        "_account_usage_all": [{"provider": "openai-codex", "fetched_at": datetime.now(timezone.utc).isoformat(), "windows": []}],
    }

    def slow_emit(*args):
        emitted.append(args)
        emit_started.set()
        release_emit.wait(timeout=2)

    monkeypatch.setattr(server, "_emit", slow_emit)
    monkeypatch.setattr(server, "_sessions", {"sid": session})
    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda _session: contextlib.nullcontext())
    from agent import quota_state
    monkeypatch.setattr(quota_state, "refresh", lambda _provider: "fresh")

    tick = threading.Thread(target=entry._publish_quota_usage)
    tick.start()
    assert emit_started.wait(timeout=1)

    started = []
    caller = threading.Thread(target=lambda: started.append(server._refresh_account_usage_async("sid", session)))
    caller.start()
    caller.join(timeout=0.2)

    assert not caller.is_alive()

    release_emit.set()
    tick.join(timeout=2)
    caller.join(timeout=2)
    if started and started[0] is not None:
        started[0].join(timeout=2)
        assert not started[0].is_alive()

    assert not caller.is_alive()
    assert started and started[0] is not None


def test_quota_usage_seed_does_not_hold_refresh_lock_while_reading_profile_state(monkeypatch):
    import contextlib

    from tui_gateway import entry

    entered = threading.Event()
    release = threading.Event()
    session = {
        "agent": None,
        "profile_home": "/profiles/secondary",
        "_account_usage_refresh_lock": threading.Lock(),
    }

    @contextlib.contextmanager
    def slow_scope(_session):
        entered.set()
        release.wait(timeout=2)
        yield

    monkeypatch.setattr(server, "_session_profile_runtime_scope", slow_scope)
    monkeypatch.setattr(server, "_account_usage_all_wire", lambda: [])
    monkeypatch.setattr(server, "_emit", lambda *_a: None)
    monkeypatch.setattr(server, "_sessions", {"sid": session})

    seed = threading.Thread(target=lambda: entry._publish_quota_usage(seed=True))
    seed.start()
    assert entered.wait(timeout=1)

    refresh_lock = session["_account_usage_refresh_lock"]
    assert refresh_lock.acquire(blocking=False)
    refresh_lock.release()

    release.set()
    seed.join(timeout=2)
    assert not seed.is_alive()


def test_pre_agent_usage_snapshot_carries_subscription_groups(monkeypatch):
    groups = [{"provider": "anthropic", "windows": [{"period": "7d", "used_percent": 40.0}]}]
    monkeypatch.setattr(server, "_account_usage_all_wire", lambda: groups)
    usage = server._session_usage_snapshot({"agent": None})
    assert usage["account_usage_all"] == groups
    assert usage["account_usage_active"] is None

    assert server._format_live_usage_output("sid", {"agent": None}, "") == server._NO_AGENT_USAGE


def test_quota_bootstrap_publishes_usage_to_live_sessions(monkeypatch):
    from agent import quota_state
    from tui_gateway import entry

    groups = [{"provider": "openai-codex", "windows": [{"period": "5h", "used_percent": 3.0}]}]
    monkeypatch.setattr(quota_state, "bootstrap_connected", lambda *a, **k: {"openai-codex": "fetched"})
    monkeypatch.setattr(server, "_account_usage_all_wire", lambda: groups)
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_sessions", {"sid-a": {"agent": None}, "sid-b": {"agent": _agent()}})

    entry._start_quota_bootstrap().join(5)

    by_sid = {sid: payload["usage"] for event, sid, payload in emitted if event == "session.usage"}
    assert set(by_sid) == {"sid-a", "sid-b"}
    assert by_sid["sid-a"]["account_usage_all"] == groups
    assert by_sid["sid-b"]["account_usage_all"] == groups


def test_quota_usage_seed_reads_provider_groups_once_for_all_live_sessions(monkeypatch):
    from tui_gateway import entry

    groups = [{"provider": "openai-codex", "windows": [{"period": "5h", "used_percent": 3.0}]}]
    reads = []
    emitted = []
    monkeypatch.setattr(server, "_account_usage_all_wire", lambda: reads.append(1) or groups)
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_sessions", {"sid-a": {"agent": None}, "sid-b": {"agent": _agent()}})

    entry._publish_quota_usage(seed=True)

    assert reads == [1]
    by_sid = {sid: payload["usage"] for event, sid, payload in emitted if event == "session.usage"}
    assert set(by_sid) == {"sid-a", "sid-b"}
    assert all(usage["account_usage_all"] == groups for usage in by_sid.values())


def test_quota_usage_clock_notifies_live_sessions_from_one_daemon_timer(monkeypatch):
    import threading

    from tui_gateway import entry

    published = threading.Event()
    calls = []
    monkeypatch.setattr(entry, "_publish_quota_usage", lambda: calls.append(1) or published.set())

    stop, thread = entry._start_quota_usage_clock(interval=0.01)
    assert published.wait(timeout=1)
    stop.set()
    thread.join(timeout=1)

    assert calls
    assert thread.daemon
    assert not thread.is_alive()


def test_quota_usage_seed_reads_once_per_profile_under_session_scope(monkeypatch):
    from contextlib import contextmanager

    from tui_gateway import entry

    active_home = ["unscoped"]
    reads = []
    emitted = []

    @contextmanager
    def scope(session):
        previous = active_home[0]
        active_home[0] = session.get("profile_home") or None
        try:
            yield
        finally:
            active_home[0] = previous

    def groups():
        home = active_home[0]
        reads.append(home)
        return [{"provider": f"provider-{home or 'launch'}", "windows": []}]

    monkeypatch.setattr(server, "_session_profile_runtime_scope", scope)
    monkeypatch.setattr(server, "_account_usage_all_wire", groups)
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_sessions", {
        "sid-a": {"agent": None},
        "sid-b": {"agent": None, "profile_home": "/profiles/b"},
        "sid-c": {"agent": None, "profile_home": "/profiles/b"},
    })

    entry._publish_quota_usage(seed=True)

    assert reads == [None, "/profiles/b"]
    by_sid = {sid: payload["usage"] for event, sid, payload in emitted if event == "session.usage"}
    assert by_sid["sid-a"]["account_usage_all"][0]["provider"] == "provider-launch"
    assert by_sid["sid-b"]["account_usage_all"][0]["provider"] == "provider-/profiles/b"
    assert by_sid["sid-c"]["account_usage_all"][0]["provider"] == "provider-/profiles/b"


def test_quota_usage_tick_reages_a_session_cache_without_reading_provider_state(monkeypatch):
    from datetime import timedelta

    from tui_gateway import entry

    fetched_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    cached_groups = [{
        "provider": "openai-codex",
        "fetched_at": fetched_at.isoformat(),
        "age_s": 0,
        "windows": [{"period": "5h", "used_percent": 3.0}],
    }]
    emitted = []

    def unexpected_provider_read():
        raise AssertionError("idle tick must not read provider state")

    monkeypatch.setattr(server, "_account_usage_all_wire", unexpected_provider_read)
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_sessions", {"sid": {"agent": None, "_account_usage_all": cached_groups}})

    entry._publish_quota_usage()

    usage = emitted[0][2]["usage"]
    assert usage["account_usage_all"][0]["fetched_at"] == fetched_at.isoformat()
    assert usage["account_usage_all"][0]["age_s"] >= 120


def test_quota_usage_seed_replaces_an_empty_prebootstrap_session_cache(monkeypatch):
    from tui_gateway import entry

    groups = [{"provider": "openai-codex", "windows": [{"period": "5h", "used_percent": 3.0}]}]
    emitted = []
    monkeypatch.setattr(server, "_account_usage_all_wire", lambda: groups)
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_sessions", {"sid": {"agent": None, "_account_usage_all": []}})

    entry._publish_quota_usage(seed=True)

    assert emitted[0][2]["usage"]["account_usage_all"] == groups


def test_quota_usage_tick_skips_a_session_while_post_turn_refresh_is_active(monkeypatch):
    from tui_gateway import entry

    cached_groups = [{"provider": "openai-codex", "fetched_at": datetime.now(timezone.utc).isoformat(), "windows": []}]
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_sessions", {
        "sid": {
            "agent": None,
            "_account_usage_all": cached_groups,
            "_account_usage_refreshing": True,
        }
    })

    entry._publish_quota_usage()

    assert emitted == []


def test_quota_usage_tick_waits_for_a_newer_post_turn_snapshot(monkeypatch):
    from tui_gateway import entry

    old_groups = [{"provider": "old", "fetched_at": "2026-01-01T00:00:00+00:00", "windows": []}]
    new_groups = [{"provider": "new", "fetched_at": "2026-01-01T00:01:00+00:00", "windows": []}]
    refresh_lock = threading.Lock()
    session = {"agent": None, "_account_usage_all": old_groups, "_account_usage_refresh_lock": refresh_lock}
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_sessions", {"sid": session})

    refresh_lock.acquire()
    tick = threading.Thread(target=entry._publish_quota_usage)
    tick.start()
    session["_account_usage_all"] = new_groups
    refresh_lock.release()
    tick.join(timeout=1)

    assert not tick.is_alive()
    assert emitted[0][2]["usage"]["account_usage_all"][0]["provider"] == "new"


def test_quota_usage_service_starts_bootstrap_and_clock_once(monkeypatch):
    from tui_gateway import entry

    started = []
    monkeypatch.setattr(entry, "_quota_usage_service_started", False)
    monkeypatch.setattr(entry, "ensure_quota_bootstrap_for_session", lambda: started.append("bootstrap"))
    monkeypatch.setattr(entry, "_start_quota_usage_clock", lambda: started.append("clock"))

    entry._ensure_quota_usage_service()
    entry._ensure_quota_usage_service()

    assert started == ["bootstrap", "clock"]


def test_explicit_quota_groups_override_compute_host_mirror_usage():
    mirrored_groups = [{"provider": "old", "age_s": 99, "windows": []}]
    fresh_groups = [{"provider": "openai-codex", "fetched_at": "2026-01-01T00:00:00+00:00", "age_s": 2, "windows": []}]
    session = {
        "agent": None,
        "_compute_host_active": True,
        "_metadata_mirror": {"usage": {"total": 42, "account_usage_all": mirrored_groups}},
    }

    usage = server._session_usage_snapshot(session, account_usage_groups=fresh_groups)

    assert usage["total"] == 42
    assert usage["account_usage_all"] == fresh_groups
    assert session["_account_usage_all"] == fresh_groups
