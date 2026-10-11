"""Quota-aware credential selection (spec D13/D23/D25/D26/D28/D33/D34/D35/D38/D41/D42/D47/D49).

Behaviour contracts for the per-account FACTS store, the per-profile VIEW reader, the
``QuotaRefresher`` and the billed-call fingerprint attribution. Covers verification.md
sections 2, 3, 4, 5 and 7. Real imports, temp quota dirs (and two HERMES_HOMEs
for profile scope); nothing reads a source file's text.
"""

from __future__ import annotations

import gc
import json
import threading
import time
import weakref
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent import quota_aware_state as st
from agent import quota_state as qs
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
PROVIDER = "opencode-go"


class _Owner:
    """A bare, hashable, weak-referenceable stand-in for a live quota_aware pool."""


def _live_refresher_threads():
    return [t for t in threading.enumerate() if t.name == "quota-refresher" and t.is_alive()]


@pytest.fixture
def qdir(tmp_path, monkeypatch):
    """Shared, cross-profile quota dir with no real network/credential access."""
    root = tmp_path / "quota"
    monkeypatch.setattr(qs, "quota_dir", lambda: root)
    monkeypatch.setattr(qs, "default_fetcher", lambda provider: pytest.fail("real fetch"))
    monkeypatch.setattr(qs, "default_connected", lambda provider: True)
    monkeypatch.setattr(qs, "default_multi_account", lambda provider: False)
    monkeypatch.setattr(qs, "credential_files", lambda: [])
    qs._last_touch.clear()
    qs._connected_cache.clear()
    qs.stop_refresher()
    with qs._pending_lock:
        qs._pending_refreshes.clear()
    yield root
    qs.stop_refresher()
    with qs._pending_lock:
        qs._pending_refreshes.clear()


@pytest.fixture
def quota_aware(monkeypatch):
    """Force the provider onto the ``quota_aware`` strategy without touching config."""
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)


def _facts_window(period="7d", percent=0.0, reset_days=3, status=None):
    return {
        "period": period,
        "used_percent": percent,
        "status": status,
        "reset_at": (NOW + timedelta(days=reset_days)).isoformat(),
        "window_seconds": 7 * 86400,
    }


def _store(provider=PROVIDER, fp="sha256:aaa", windows=None, *, entries=None, fetched=True,
           error=None, backoff_until=None, metered_exhausted_until=qs._UNSET, when=NOW):
    return qs.store_account_facts(
        provider, fp, windows if windows is not None else [_facts_window()],
        entries=entries or [{"profile": "default", "entry_id": fp[-3:], "label": fp[-3:]}],
        fetched_at=when.isoformat() if fetched else None,
        attempted_at=when.isoformat(),
        error=error, backoff_until=backoff_until,
        metered_exhausted_until=metered_exhausted_until, now=when,
    )


# ============================================================================
# Window and state derivation (D9, D25, D28, D47)
# ============================================================================

def test_status_rate_limited_wins_over_low_percent():
    """Fixture: ``weekly.status = "rate-limited"`` with ``percent = 91`` -> spent."""
    window = AccountUsageWindow("Weekly", used_percent=91.0,
                                reset_at=NOW + timedelta(days=3), status="rate-limited")
    assert st.window_spent(window, NOW) is True


def test_status_wins_over_percent_both_directions():
    future = NOW + timedelta(days=3)
    # status says ok even though percent >= 100 -> not spent
    assert st.window_spent({"used_percent": 100.0, "status": "ok", "reset_at": future.isoformat()}, NOW) is False
    # no status -> percent is the fallback
    assert st.window_spent({"used_percent": 100.0, "status": None, "reset_at": future.isoformat()}, NOW) is True
    assert st.window_spent({"used_percent": 42.0, "status": None, "reset_at": future.isoformat()}, NOW) is False


def test_expired_window_rolls_forward_on_the_clock():
    past = (NOW - timedelta(hours=1)).isoformat()
    assert st.window_spent({"used_percent": 100.0, "status": "rate-limited", "reset_at": past}, NOW) is False


def test_one_spent_window_makes_account_non_covered():
    windows = [_facts_window(period="5h", percent=0.0),
               _facts_window(period="7d", percent=100.0)]
    assert st.account_state(windows, metered=False, now=NOW) == "exhausted"
    assert st.account_state(windows, metered=True, now=NOW) == "overflow"
    # recovery = LATEST spent reset
    assert st.effective_recovery(windows, None, NOW) == st.as_dt(windows[1]["reset_at"])


def test_refusal_outranks_stale_plan_evidence_and_recovery_is_max():
    windows = [_facts_window(percent=100.0, reset_days=3)]  # plan spent, resets in 3d
    refusal = NOW + timedelta(hours=2)
    assert st.account_state(windows, metered=True, metered_exhausted_until=refusal, now=NOW) == "exhausted"
    # effective recovery = max(plan spent reset, refusal) -> the later plan reset wins
    assert st.effective_recovery(windows, refusal, NOW) == st.as_dt(windows[0]["reset_at"])
    # and with a refusal later than the plan reset, the refusal wins
    late = NOW + timedelta(days=5)
    assert st.effective_recovery(windows, late, NOW) == late


def test_d47_reviewer_case_ordering_by_effective_recovery():
    # A: plan reset 300s, cushion until 110s. B: plan reset 200s, cushion until 190s.
    a = {"used_percent": 100.0, "status": None, "reset_at": (NOW + timedelta(seconds=300)).isoformat()}
    b = {"used_percent": 100.0, "status": None, "reset_at": (NOW + timedelta(seconds=200)).isoformat()}
    a_until = NOW + timedelta(seconds=110)
    b_until = NOW + timedelta(seconds=190)
    at100 = {fp: st.effective_recovery([w], until, NOW)
             for fp, w, until in (("A", a, a_until), ("B", b, b_until))}
    assert sorted(at100, key=lambda k: at100[k]) == ["B", "A"]
    at110 = NOW + timedelta(seconds=110)
    # A's cushion expired -> A overflow; B still exhausted
    assert st.account_state([a], metered=True, metered_exhausted_until=a_until, now=at110) == "overflow"
    assert st.account_state([b], metered=True, metered_exhausted_until=b_until, now=at110) == "exhausted"


def test_refusal_recording_rule_both_directions(qdir):
    """D28/D47 unified: recorded when the account was ``overflow`` at refusal time; a refusal
    while ``covered`` records nothing."""
    _store(fp="sha256:aaa", windows=[_facts_window(percent=100.0)])
    # covered -> nothing recorded
    assert qs.record_refusal(PROVIDER, "sha256:aaa", state="covered",
                             reset_at=NOW + timedelta(hours=1), now=NOW) is False
    assert qs.read_account_facts(PROVIDER, "sha256:aaa")["metered_exhausted_until"] is None
    # overflow (plan already spent - the motivating case) -> recorded
    assert qs.record_refusal(PROVIDER, "sha256:aaa", state="overflow",
                             reset_at=NOW + timedelta(hours=1), now=NOW) is True
    stored = qs.read_account_facts(PROVIDER, "sha256:aaa")["metered_exhausted_until"]
    assert st.as_dt(stored) == NOW + timedelta(hours=1)


# ============================================================================
# Facts, views, writers, readers (D13, D23, D33, D34, D35, D41)
# ============================================================================

def test_two_profiles_same_key_one_facts_record(qdir):
    _store(entries=[{"profile": "alpha", "entry_id": "a1", "label": "OPENCODE_GO_API_KEY_2"}])
    _store(entries=[{"profile": "beta", "entry_id": "b1", "label": "OPENCODE_GO_API_KEY_2"}])
    files = list((qs.accounts_root() / PROVIDER).glob("*.json"))
    assert len(files) == 1
    record = qs.read_account_facts(PROVIDER, "sha256:aaa")
    profiles = {e["profile"] for e in record["entries"]}
    assert profiles == {"alpha", "beta"}


def test_facts_record_is_facts_only(qdir):
    _store(metered_exhausted_until=NOW + timedelta(hours=1))
    record = qs.read_account_facts(PROVIDER, "sha256:aaa")
    forbidden = {"metered", "state", "serving", "serving_since", "states", "history", "strategy", "read_health"}
    assert forbidden.isdisjoint(record)
    assert set(record) == {
        "provider", "fingerprint", "entries", "windows", "metered_exhausted_until",
        "fetched_at", "attempted_at", "error", "backoff_until",
    }


# ============================================================================
# D23: policy-shaped values are derived AT READ TIME (never a stale view cache)
# ============================================================================

def test_read_view_reflects_policy_edits_without_a_rewrite(qdir, monkeypatch):
    """Editing credential_pool_policy changes the NEXT read_view with no update_view call:
    a persisted view's metered/state are a cache, never an input (spec D23)."""
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == "anthropic")
    _store(provider="anthropic", fp="sha256:spent", windows=[_facts_window(percent=100.0)],
           entries=[{"profile": "default", "entry_id": "s", "label": "SPENT_KEY"}])
    config = {"credential_pool_policy": {"anthropic": {"metered_overflow": ["SPENT_KEY"]}}}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    # One write persists a view; its metered=True is now only a display cache.
    written = qs.update_view("default", "anthropic", now=NOW)
    assert (written["accounts"][0]["metered"], written["accounts"][0]["state"]) == (True, "overflow")
    # Remove the target from config - the SAME facts must read as not-metered, immediately.
    config["credential_pool_policy"]["anthropic"]["metered_overflow"] = []
    after_remove = qs.read_view("default", "anthropic", now=NOW)
    assert (after_remove["accounts"][0]["metered"], after_remove["accounts"][0]["state"]) == (False, "exhausted")
    # Add a target back - read_view flips again, still with no rewrite.
    config["credential_pool_policy"]["anthropic"]["metered_overflow"] = ["SPENT_KEY"]
    after_add = qs.read_view("default", "anthropic", now=NOW)
    assert (after_add["accounts"][0]["metered"], after_add["accounts"][0]["state"]) == (True, "overflow")


def test_read_view_two_profiles_different_configs_same_facts(qdir, monkeypatch):
    """Two profiles with different credential_pool_policy read different metered from the SAME
    shared facts (D23/D33): the classification follows the READING profile's own config."""
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == "anthropic")
    _store(provider="anthropic", fp="sha256:spent", windows=[_facts_window(percent=100.0)],
           entries=[{"profile": p, "entry_id": p, "label": "SPENT_KEY"} for p in ("alpha", "beta")])
    facts_before = qs.read_account_facts("anthropic", "sha256:spent")
    configs = {
        "alpha": {"credential_pool_policy": {"anthropic": {"metered_overflow": ["SPENT_KEY"]}}},
        "beta": {"credential_pool_policy": {}},
    }
    active = {"profile": "alpha"}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: configs[active["profile"]])

    def read(profile):
        active["profile"] = profile
        return qs.read_view(profile, "anthropic", now=NOW)

    view_a = read("alpha")
    view_b = read("beta")
    assert (view_a["accounts"][0]["metered"], view_a["accounts"][0]["state"]) == (True, "overflow")
    assert (view_b["accounts"][0]["metered"], view_b["accounts"][0]["state"]) == (False, "exhausted")
    # Neither read wrote the shared facts, and beta has no view file of its own.
    assert qs.read_account_facts("anthropic", "sha256:spent") == facts_before
    assert not qs.view_path("beta", "anthropic").exists()


def test_facts_record_stays_facts_only_across_a_policy_edit(qdir, monkeypatch):
    """A policy edit changes the derived view but never leaks a policy-shaped field into the
    shared facts record (D23)."""
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == "anthropic")
    _store(provider="anthropic", fp="sha256:spent", windows=[_facts_window(percent=100.0)],
           entries=[{"profile": "default", "entry_id": "s", "label": "SPENT_KEY"}])
    before = qs.read_account_facts("anthropic", "sha256:spent")
    config = {"credential_pool_policy": {"anthropic": {"metered_overflow": ["SPENT_KEY"]}}}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    qs.update_view("default", "anthropic", now=NOW)
    config["credential_pool_policy"]["anthropic"]["metered_overflow"] = []
    qs.read_view("default", "anthropic", now=NOW)
    after = qs.read_account_facts("anthropic", "sha256:spent")
    forbidden = {"metered", "state", "serving", "serving_since", "states", "history", "strategy", "read_health"}
    assert forbidden.isdisjoint(after)
    assert after == before


def test_replacing_key_creates_new_facts_record(qdir):
    _store(fp="sha256:aaa")
    _store(fp="sha256:bbb")
    assert len(list((qs.accounts_root() / PROVIDER).glob("*.json"))) == 2


def test_per_entry_fetch_writes_only_facts(qdir):
    snapshot = AccountUsageSnapshot(
        provider=PROVIDER, source="t", fetched_at=NOW,
        windows=(AccountUsageWindow("Weekly", 30.0, NOW + timedelta(days=3), status="ok"),))
    assert qs.fetch_account_facts(PROVIDER, "sha256:aaa", fetcher=lambda: snapshot, now=NOW) == "fetched"
    assert qs.account_facts_path(PROVIDER, "sha256:aaa").exists()
    assert not (qdir / "snapshot").exists()
    assert not (qdir / "views").exists()
    record = qs.read_account_facts(PROVIDER, "sha256:aaa")
    assert record["windows"][0]["period"] == "7d"
    assert record["windows"][0]["status"] == "ok"


def test_unmapped_label_is_preserved_not_dropped(qdir):
    snapshot = AccountUsageSnapshot(
        provider="openrouter", source="t", fetched_at=NOW,
        windows=(AccountUsageWindow("API key quota", 25.0, None, detail="x"),))
    windows = qs._windows_from_snapshot(snapshot)
    assert windows and windows[0]["period"] == "api key quota"


def test_non_quota_aware_provider_has_no_view_and_legacy_snapshot_unchanged(qdir):
    _store(provider="anthropic", fp="sha256:anthropic",
           windows=[_facts_window(period="7d", percent=10.0)],
           entries=[{"profile": "default", "entry_id": "a", "label": "a"}])
    assert qs.read_view("default", "anthropic") is None
    # multi-account collapse still applies for a non-quota_aware provider
    qs._store_multi_account("anthropic", NOW)
    assert qs.read_snapshot("anthropic")["multi_account"] is True


def test_quota_aware_multi_account_does_not_blank(qdir, quota_aware):
    _store(fp="sha256:aaa")
    qs._store_multi_account(PROVIDER, NOW)
    assert not (qdir / "snapshot" / f"{PROVIDER}.json").exists()
    assert qs.read_account_facts(PROVIDER, "sha256:aaa") is not None
    # the legacy multi-account detector is also gated off for a quota_aware provider
    assert qs.default_multi_account(PROVIDER) is False


def test_profile_isolation_different_policies_same_facts(qdir, quota_aware, monkeypatch):
    # anthropic: bills_when_plan_spent is False, so only a policy target makes it metered.
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == "anthropic")
    _store(provider="anthropic", fp="sha256:spent",
           windows=[_facts_window(percent=100.0)],
           entries=[{"profile": p, "entry_id": p, "label": "SPENT_KEY"} for p in ("alpha", "beta")])
    facts_before = qs.read_account_facts("anthropic", "sha256:spent")
    view_a = qs.update_view("alpha", "anthropic", now=NOW,
                            policy={"metered_overflow": ["SPENT_KEY"]})
    view_b = qs.update_view("beta", "anthropic", now=NOW, policy={})
    assert view_a["accounts"][0]["state"] == "overflow"     # configured metered cushion
    assert view_b["accounts"][0]["state"] == "exhausted"    # no cushion configured
    # writing beta left alpha's view and the shared facts untouched
    assert qs.read_view("alpha", "anthropic", policy={"metered_overflow": ["SPENT_KEY"]})["accounts"][0]["state"] == "overflow"
    assert qs.read_account_facts("anthropic", "sha256:spent") == facts_before


def test_serving_windows_equal_that_accounts_facts(qdir, quota_aware):
    _store(fp="sha256:aaa", windows=[_facts_window(percent=12.0)])
    view = qs.update_view("default", PROVIDER, now=NOW, policy={})
    assert view["serving"] == "sha256:aaa"
    assert view["windows"] == qs.read_account_facts(PROVIDER, "sha256:aaa")["windows"]
    assert view["windows"] != []  # not a sum/aggregate


def test_history_restarts_when_serving_changes(qdir, quota_aware):
    _store(fp="sha256:aaa", windows=[_facts_window(percent=10.0)])
    _store(fp="sha256:bbb", windows=[_facts_window(percent=20.0)])
    first = qs.update_view("default", PROVIDER, now=NOW, policy={})
    assert first["serving"] == "sha256:aaa"
    assert {s["fingerprint"] for s in first["history"]} == {"sha256:aaa"}
    # A disappears (key replaced) -> serving moves to B; the series must not chain A->B
    qs.account_facts_path(PROVIDER, "sha256:aaa").unlink()
    later = NOW + timedelta(minutes=5)
    second = qs.update_view("default", PROVIDER, now=later, policy={})
    assert second["serving"] == "sha256:bbb"
    assert {s["fingerprint"] for s in second["history"]} == {"sha256:bbb"}
    assert second["serving_since"] != first["serving_since"]


def test_view_history_growth_is_bounded_across_bootstraps(qdir, quota_aware):
    """Many bootstrap cycles (each ``update_view`` on the ``load_pool(quota_bootstrap=True)``
    request path) with changing served windows must keep the history sample count and the view
    file within a fixed bound, not grow one sample per call (D35)."""
    path = qs.view_path("default", PROVIDER)
    for i in range(300):
        _store(fp="sha256:aaa", windows=[_facts_window(percent=float(i % 90))],
               when=NOW + timedelta(seconds=i))
        qs.update_view("default", PROVIDER, now=NOW + timedelta(seconds=i), policy={})
    view = qs._read_view_raw("default", PROVIDER)
    assert view is not None
    assert len(view["history"]) <= 64, "the history grew without a bound"
    assert len(view["history"]) <= qs.VIEW_HISTORY_MAX_SAMPLES
    assert path.stat().st_size < 64 * 1024


def test_repeated_bootstrap_with_unchanged_facts_adds_no_sample(qdir, quota_aware):
    """A bootstrap that recomposes from UNCHANGED facts records no new sample, so a burst of
    billed aux calls against one stable serving account cannot grow the series (D35)."""
    _store(fp="sha256:aaa", windows=[_facts_window(percent=10.0)])
    first = qs.update_view("default", PROVIDER, now=NOW, policy={})
    assert len(first["history"]) == 1
    for i in range(1, 60):
        qs.update_view("default", PROVIDER, now=NOW + timedelta(seconds=i), policy={})
    view = qs._read_view_raw("default", PROVIDER)
    assert view is not None
    assert len(view["history"]) == 1


def test_pool_writer_publishes_only_a_view(qdir, quota_aware, monkeypatch):
    _store(fp="sha256:aaa")
    from agent.credential_pool import CredentialPool, PooledCredential
    entry = PooledCredential(provider=PROVIDER, id="a1", label="A", auth_type="api_key",
                             priority=0, source="env:A", access_token="k")
    entry.extra["secret_fingerprint"] = "sha256:aaa"
    pool = CredentialPool(PROVIDER, [entry])
    view = pool.sync_quota_view("default")
    assert view is not None
    assert qs.view_path("default", PROVIDER).exists()
    assert not (qdir / "snapshot").exists()


# ============================================================================
# Fail-open and read health (D16, D26)
# ============================================================================

def test_fail_open_with_and_without_prior_evidence(qdir, quota_aware):
    # prior exhausted evidence, then a failed read -> still non-covered, read_health error
    _store(fp="sha256:spent", windows=[_facts_window(percent=100.0)])
    qs.store_account_facts(PROVIDER, "sha256:spent", [_facts_window(percent=100.0)],
                           attempted_at=NOW.isoformat(),
                           error={"status": 500, "message": "boom", "backoff_s": 60},
                           backoff_until=(NOW + timedelta(seconds=60)).isoformat(), now=NOW)
    # no prior evidence -> covered but behind; read_health error
    qs.store_account_facts(PROVIDER, "sha256:fresh", [], attempted_at=NOW.isoformat(),
                           error={"status": 500, "message": "boom", "backoff_s": 60},
                           backoff_until=(NOW + timedelta(seconds=60)).isoformat(), now=NOW)
    view = qs.read_view("default", PROVIDER, now=NOW, policy={})
    by_fp = {a["fingerprint"]: a for a in view["accounts"]}
    assert by_fp["sha256:spent"]["state"] in {"overflow", "exhausted"}
    assert by_fp["sha256:fresh"]["state"] == "covered"
    assert by_fp["sha256:spent"]["read_health"] == "error"
    assert by_fp["sha256:fresh"]["read_health"] == "error"


def test_unknown_only_when_no_evidence_ever(qdir, quota_aware):
    # No facts record at all -> the account is simply absent (no fabricated state).
    view = qs.read_view("default", PROVIDER, now=NOW, policy={})
    assert view["accounts"] == []
    assert view["states"] == {"covered": 0, "overflow": 0, "exhausted": 0}


def test_read_health_error_with_state_preserved(qdir, quota_aware):
    _store(fp="sha256:aaa", windows=[_facts_window(percent=100.0)])
    qs.store_account_facts(PROVIDER, "sha256:aaa", [_facts_window(percent=100.0)],
                           attempted_at=NOW.isoformat(),
                           error={"status": 429, "message": "rate limited", "backoff_s": 300},
                           backoff_until=(NOW + timedelta(seconds=300)).isoformat(), now=NOW)
    view = qs.read_view("default", PROVIDER, now=NOW, policy={})
    acct = view["accounts"][0]
    assert acct["read_health"] == "error"
    assert acct["state"] in {"overflow", "exhausted"}  # state kept, not "unknown"
    assert acct["backoff_until"] is not None


# ============================================================================
# Refresher, attribution, bounds (D42)
# ============================================================================

def test_no_fetch_on_request_path(qdir, quota_aware, monkeypatch):
    monkeypatch.setattr(qs, "fetch_account_facts",
                        lambda *a, **k: pytest.fail("fetch on a request path"))
    from agent.credential_pool import CredentialPool
    pool = CredentialPool(PROVIDER, [])
    pool.select()                       # selection is pure
    qs.read_view("default", PROVIDER)   # read is pure


def test_refresher_starts_and_stops_with_the_pool(qdir, monkeypatch):
    monkeypatch.setattr("agent.credential_pool.get_pool_strategy", lambda provider: "quota_aware")
    from agent.credential_pool import load_pool
    pool = load_pool(PROVIDER)
    assert qs.is_refresher_running() is True
    pool.stop_quota_refresher()
    assert qs.is_refresher_running() is False


def test_bare_pool_does_not_start_refresher(qdir, monkeypatch):
    monkeypatch.setattr("agent.credential_pool.get_pool_strategy", lambda provider: "fill_first")
    from agent.credential_pool import load_pool
    load_pool(PROVIDER)
    assert qs.is_refresher_running() is False


def _mk_entry(entry_id, label, key, fingerprint, base_url):
    from agent.credential_pool import PooledCredential
    entry = PooledCredential(provider=PROVIDER, id=entry_id, label=label, auth_type="api_key",
                             priority=0, source=f"env:{label}", access_token=key, base_url=base_url)
    entry.extra["secret_fingerprint"] = fingerprint
    return entry


# Real opencode-go accounts share ONE base_url (spec.md Current State), so a base_url match
# can never tell the accounts apart. Attribution must come from the pool's serving state
# (the entry the session is bound to / last leased), never from the URL (D26/D36/D42).
SHARED_URL = "https://opencode.ai/zen/go/v1"


def _two_account_pool(monkeypatch, *, serving=None, shared_url=SHARED_URL):
    """Two accounts on ONE base_url, optionally bound to *serving* via the pool's real lease
    API (``acquire_lease``), which is what a live session actually holds."""
    from agent.credential_pool import CredentialPool
    a = _mk_entry("a", "OPENCODE_GO_API_KEY_2", "kA", "sha256:aaa", shared_url)
    b = _mk_entry("b", "OPENCODE_GO_API_KEY", "kB", "sha256:bbb", shared_url)
    pool = CredentialPool(PROVIDER, [a, b])
    if serving is not None:
        assert pool.acquire_lease(serving) == serving
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider, **kw: pool)
    return pool


@pytest.fixture
def two_account_pool(qdir, monkeypatch):
    """Two accounts sharing one base_url, with the session bound to entry ``b``."""
    return _two_account_pool(monkeypatch, serving="b")


def _drive_and_pending(fn):
    qs._last_touch.clear()
    with qs._pending_lock:
        qs._pending_refreshes.clear()
    fn()
    return qs.pending_refreshes()


def test_attribution_turn_usage_path(two_account_pool):
    from agent import turn_usage
    agent = SimpleNamespace(provider=PROVIDER, base_url=SHARED_URL,
                            api_key="kB", _credential_pool_entry_id="b")
    pending = _drive_and_pending(lambda: turn_usage._mark_quota_dirty(agent))
    assert (PROVIDER, "sha256:bbb") in pending


def test_attribution_aux_accounting_path(two_account_pool):
    from agent.aux_accounting import record_aux_usage
    pending = _drive_and_pending(lambda: record_aux_usage(
        SimpleNamespace(usage=None), "title_generation",
        provider=PROVIDER, base_url=SHARED_URL))
    assert (PROVIDER, "sha256:bbb") in pending


def test_attribution_hermes_state_path(two_account_pool, tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s1", source="cli", model="m")
        pending = _drive_and_pending(lambda: db.record_auxiliary_usage(
            "s1", "background_review", model="m", billing_provider=PROVIDER,
            billing_base_url=SHARED_URL, input_tokens=5))
    finally:
        db.close()
    assert (PROVIDER, "sha256:bbb") in pending


def test_attribution_serving_entry_beats_a_base_url_hint(qdir, monkeypatch):
    """A base_url hint that uniquely names a DIFFERENT account must not win: the pool's serving
    state is the truth."""
    from agent.credential_pool import CredentialPool
    a = _mk_entry("a", "OPENCODE_GO_API_KEY_2", "kA", "sha256:aaa", "https://a.example/zen/go/v1")
    b = _mk_entry("b", "OPENCODE_GO_API_KEY", "kB", "sha256:bbb", "https://b.example/zen/go/v1")
    pool = CredentialPool(PROVIDER, [a, b])
    assert pool.acquire_lease("b") == "b"
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider, **kw: pool)
    from agent.aux_accounting import record_aux_usage
    pending = _drive_and_pending(lambda: record_aux_usage(
        SimpleNamespace(usage=None), "title_generation",
        provider=PROVIDER, base_url="https://a.example/zen/go/v1"))
    assert (PROVIDER, "sha256:bbb") in pending
    assert (PROVIDER, "sha256:aaa") not in pending


def test_attribution_unknown_never_guesses_another_account(qdir, monkeypatch):
    """No serving binding and a shared base_url: the served account cannot be identified, so
    nothing is recorded - never one account's fingerprint (D26/D36/D42)."""
    _two_account_pool(monkeypatch, serving=None)
    from agent.aux_accounting import record_aux_usage
    pending = _drive_and_pending(lambda: record_aux_usage(
        SimpleNamespace(usage=None), "title_generation",
        provider=PROVIDER, base_url=SHARED_URL))
    assert pending == set()
    assert (PROVIDER, "sha256:aaa") not in pending and (PROVIDER, "sha256:bbb") not in pending


def test_attribution_single_account_resolves_without_a_lease(qdir, monkeypatch):
    """One account is unambiguous: it is attributed even before any selection is recorded."""
    from agent.credential_pool import CredentialPool
    solo = _mk_entry("solo", "OPENCODE_GO_API_KEY", "kOnly", "sha256:solo", SHARED_URL)
    pool = CredentialPool(PROVIDER, [solo])
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider, **kw: pool)
    from agent.aux_accounting import record_aux_usage
    pending = _drive_and_pending(lambda: record_aux_usage(
        SimpleNamespace(usage=None), "title_generation",
        provider=PROVIDER, base_url=SHARED_URL))
    assert (PROVIDER, "sha256:solo") in pending


# ---------------------------------------------------------------------------
# Production-path attribution: a REAL credential pool (two env-seeded opencode-go
# accounts sharing ONE base_url) driven through the REAL aux / hermes-state /
# turn call sites. The pool is not stubbed, so ``pool.current()`` is None (a fresh
# pool per load) and only the threaded credential can name the serving account.
# ---------------------------------------------------------------------------

def _fake_aux_client(*, api_key, base_url):
    """A stand-in aux OpenAI client whose ``.api_key`` is the credential that serves —
    the outermost boundary the repo's aux tests stub; everything downstream is real."""
    from unittest.mock import MagicMock
    client = MagicMock()
    client.api_key = api_key
    client.base_url = base_url
    client.chat.completions.create.return_value = SimpleNamespace(
        model="glm-5",
        choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content="t", tool_calls=None),
                                 finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )
    return client


@pytest.fixture
def real_two_account_pool(qdir, tmp_path, monkeypatch):
    """Two REAL opencode-go credentials (env-seeded, one shared base_url) in a temp
    HERMES_HOME, so ``load_pool`` builds the production pool. Loading twice mirrors
    production: the first load persists the sanitized rows that carry
    ``secret_fingerprint``; the second reads them back (an env row re-hydrates its secret)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "kB-serving")
    monkeypatch.setenv("OPENCODE_GO_API_KEY_2", "kA-other")
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)
    monkeypatch.setattr("agent.credential_pool.get_pool_strategy", lambda provider: "quota_aware")
    from agent.credential_pool import load_pool
    load_pool(PROVIDER)          # seed + persist the sanitized rows
    pool = load_pool(PROVIDER)   # read them back WITH their fingerprints
    by_key = {}
    for entry in pool.entries():
        key = entry.runtime_api_key
        fp = entry.extra.get("secret_fingerprint")
        assert key and fp, f"unexpected real pool entry: {entry!r}"
        by_key[key] = {"fingerprint": fp, "entry_id": entry.id}
    assert set(by_key) == {"kB-serving", "kA-other"}
    assert by_key["kB-serving"]["fingerprint"] != by_key["kA-other"]["fingerprint"]
    return by_key


def test_aux_call_attributes_the_serving_account_with_a_real_pool(real_two_account_pool, monkeypatch):
    """The REAL aux production path (``call_llm`` -> the validation chokepoint) records the
    fingerprint of the account whose credential actually served, not the sibling that shares
    the one base_url (D26/D36/D42). With a fresh (unbound) pool ``served_fingerprint`` cannot
    resolve from ``pool.current()``, so only the threaded credential can name the account."""
    from agent import auxiliary_client as aux
    serving = real_two_account_pool["kB-serving"]
    other = real_two_account_pool["kA-other"]
    client = _fake_aux_client(api_key="kB-serving", base_url=SHARED_URL)
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (client, "glm-5"))
    pending = _drive_and_pending(lambda: aux.call_llm(
        task="title_generation", provider=PROVIDER, model="glm-5",
        messages=[{"role": "user", "content": "hi"}]))
    assert (PROVIDER, serving["fingerprint"]) in pending
    assert (PROVIDER, other["fingerprint"]) not in pending


def test_hermes_state_path_attributes_the_serving_account_with_a_real_pool(real_two_account_pool, tmp_path):
    """A background-review fork routes its aggregate through the hermes-state per-call
    boundary (``record_auxiliary_usage`` -> ``_record_model_usage``); the fork's served
    credential must travel with it (D42), not fall back to the unbound pool."""
    from agent.background_review import _record_review_usage_to_parent, _snapshot_review_usage
    from hermes_state import SessionDB
    serving = real_two_account_pool["kB-serving"]
    other = real_two_account_pool["kA-other"]
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s1", source="cli", model="m")
        fork = SimpleNamespace(
            model="m", provider=PROVIDER, base_url=SHARED_URL,
            api_key="kB-serving", _credential_pool_entry_id=serving["entry_id"],
            session_input_tokens=10, session_output_tokens=2,
            session_cache_read_tokens=0, session_cache_write_tokens=0,
            session_reasoning_tokens=0, session_api_calls=1,
            session_estimated_cost_usd=None,
        )
        parent = SimpleNamespace(_session_db=db, session_id="s1")
        usage = _snapshot_review_usage(fork)
        pending = _drive_and_pending(lambda: _record_review_usage_to_parent(parent, usage))
    finally:
        db.close()
    assert (PROVIDER, serving["fingerprint"]) in pending
    assert (PROVIDER, other["fingerprint"]) not in pending


def test_turn_path_attributes_the_serving_account_with_a_real_pool(real_two_account_pool):
    """No regression on the turn path: it still names the serving account with a real pool."""
    from agent import turn_usage
    serving = real_two_account_pool["kB-serving"]
    other = real_two_account_pool["kA-other"]
    agent = SimpleNamespace(provider=PROVIDER, base_url=SHARED_URL,
                            api_key="kB-serving", _credential_pool_entry_id=serving["entry_id"])
    pending = _drive_and_pending(lambda: turn_usage._mark_quota_dirty(agent))
    assert (PROVIDER, serving["fingerprint"]) in pending
    assert (PROVIDER, other["fingerprint"]) not in pending


def test_aux_call_with_unmatched_credential_records_nothing(real_two_account_pool, monkeypatch):
    """A credential that names no account in the pool must record nothing, never a guess (D26)."""
    from agent import auxiliary_client as aux
    client = _fake_aux_client(api_key="kNOT-in-the-pool", base_url=SHARED_URL)
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (client, "glm-5"))
    pending = _drive_and_pending(lambda: aux.call_llm(
        task="title_generation", provider=PROVIDER, model="glm-5",
        messages=[{"role": "user", "content": "hi"}]))
    assert pending == set()


def test_backoff_account_staleness_is_visible(qdir, quota_aware):
    _store(fp="sha256:aaa", windows=[_facts_window(percent=5.0)])
    qs.store_account_facts(PROVIDER, "sha256:aaa", [_facts_window(percent=5.0)],
                           fetched_at=None, attempted_at=NOW.isoformat(),
                           error={"status": 429, "message": "rate limited", "backoff_s": 300},
                           backoff_until=(NOW + timedelta(seconds=300)).isoformat(), now=NOW)
    view = qs.read_view("default", PROVIDER, now=NOW, policy={})
    acct = view["accounts"][0]
    assert acct["read_health"] == "error"
    assert acct["backoff_until"] is not None


def test_per_entry_fetch_uses_the_fingerprints_credential(qdir, two_account_pool, monkeypatch):
    seen = {}

    def fake_for(provider, *, api_key=None, base_url=None):
        seen["args"] = (provider, api_key, base_url)
        return AccountUsageSnapshot(provider=provider, source="t", fetched_at=NOW, windows=())

    monkeypatch.setattr(qs, "default_fetcher_for", fake_for)
    assert qs.fetch_account_facts(PROVIDER, "sha256:bbb", now=NOW) == "fetched"
    assert seen["args"] == (PROVIDER, "kB", SHARED_URL)


def test_refresher_drains_the_per_account_queue(qdir, monkeypatch):
    import time
    calls = []
    monkeypatch.setattr(qs, "fetch_account_facts",
                        lambda provider, fp, **k: calls.append((provider, fp)) or "fetched")
    qs.enqueue_refresh(PROVIDER, "sha256:bbb")
    refresher = qs.QuotaRefresher(interval_s=0.05, jitter=0.0)
    refresher.start()
    try:
        deadline = time.monotonic() + 3
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        refresher.stop()
    assert (PROVIDER, "sha256:bbb") in calls


# ============================================================================
# Capability and configuration (D15, D38, D46)
# ============================================================================

def test_opencode_go_declares_capabilities():
    from providers import get_provider_profile
    profile = get_provider_profile("opencode-go")
    assert profile.supports_per_account_usage is True
    assert profile.bills_when_plan_spent is True


def test_provider_without_capability_defaults_false():
    from providers.base import ProviderProfile
    assert ProviderProfile.supports_per_account_usage is False
    assert ProviderProfile.bills_when_plan_spent is False


def test_spent_plan_with_no_matching_target_classifies_overflow(qdir, quota_aware):
    """opencode-go bills after the plan is spent: a spent window with NO configured target
    classifies ``overflow`` (metered), not ``exhausted`` (D38/D46)."""
    _store(fp="sha256:aaa", windows=[_facts_window(percent=100.0)],
           entries=[{"profile": "default", "entry_id": "a", "label": "OPENCODE_GO_API_KEY_2"}])
    view = qs.read_view("default", PROVIDER, now=NOW, policy={})  # no metered_overflow
    assert view["accounts"][0]["metered"] is True
    assert view["accounts"][0]["state"] == "overflow"


# ============================================================================
# Profile scope: two HERMES_HOMEs -> different profile names, shared facts
# ============================================================================

def test_two_hermes_homes_two_profiles(tmp_path, monkeypatch, qdir):
    shared = qdir
    monkeypatch.setattr(qs, "quota_dir", lambda: shared)
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    for name in ("alpha", "beta"):
        home = tmp_path / ".hermes" / "profiles" / name
        home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert qs.current_profile() == name
    # both profiles read the SAME shared facts, each with its own view file
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "alpha"))
    _store(fp="sha256:aaa", entries=[{"profile": "alpha", "entry_id": "a", "label": "A"}])
    qs.update_view("alpha", PROVIDER, now=NOW, policy={})
    qs.update_view("beta", PROVIDER, now=NOW, policy={})
    assert qs.view_path("alpha", PROVIDER).exists()
    assert qs.view_path("beta", PROVIDER).exists()
    assert len(list((qs.accounts_root() / PROVIDER).glob("*.json"))) == 1
    assert json.loads(qs.view_path("alpha", PROVIDER).read_text())["provider"] == PROVIDER


# ============================================================================
# Refresher bounds, attribution and lifecycle (D41, D42, D49)
# ============================================================================

def test_refresher_fetch_count_is_bounded_over_a_simulated_period(real_two_account_pool, monkeypatch):
    """A real quota_aware pool whose load re-queues every fingerprint: over a simulated period
    the refresher fetches at most accounts x drain cycles (bounded, not once per queue item)."""
    serving = real_two_account_pool["kB-serving"]
    calls = []

    def counting(provider):
        calls.append(provider)
        return AccountUsageSnapshot(provider=provider, source="t", fetched_at=NOW, windows=())

    monkeypatch.setattr(qs, "default_fetcher", counting)
    monkeypatch.setattr(qs, "default_fetcher_for",
                        lambda provider, *, api_key=None, base_url=None: counting(provider))
    # Drive the PROCESS-WIDE refresher (the one a pool owns and a bootstrap re-queue wakes).
    qs.stop_refresher()
    qs.start_refresher(interval_s=0.05)
    try:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            # a billed call re-enqueues (never fetches) while the refresher drains
            qs.enqueue_refresh(PROVIDER, serving["fingerprint"])
            time.sleep(0.05)
    finally:
        qs.stop_refresher()
    assert calls, "the refresher never drained the queue"
    # ~10 drains x 2 accounts: the fetch count is bounded by accounts x drain cycles.
    assert len(calls) <= 2 * 60


def test_facts_fetch_honours_the_stored_backoff(qdir):
    """A 429 that set ``backoff_until`` must not be re-fetched inside the window."""
    qs.store_account_facts(
        PROVIDER, "sha256:aaa", [_facts_window(percent=5.0)], fetched_at=None,
        attempted_at=NOW.isoformat(),
        error={"status": 429, "message": "rate limited", "backoff_s": 300},
        backoff_until=(NOW + timedelta(seconds=300)).isoformat(), now=NOW)
    hits = []
    result = qs.fetch_account_facts(PROVIDER, "sha256:aaa", now=NOW + timedelta(seconds=1),
                                    fetcher=lambda: hits.append(1))
    assert result == "backoff"
    assert hits == []


def test_read_view_rolls_a_past_reset_window_back_to_covered(qdir, quota_aware):
    """An expired rate-limited window rolls forward to 0% with its spent marker cleared, so the
    account is covered and selectable again without a call."""
    window = {"period": "7d", "used_percent": 100.0, "status": "rate-limited",
              "reset_at": (NOW - timedelta(hours=1)).isoformat(), "window_seconds": 7 * 86400}
    _store(fp="sha256:aaa", windows=[window])
    acct = qs.read_view("default", PROVIDER, now=NOW, policy={})["accounts"][0]
    assert acct["state"] == "covered"
    assert acct["windows"][0]["status"] is None
    assert acct["windows"][0]["used_percent"] == 0.0


def test_one_served_call_enqueues_only_the_serving_account(real_two_account_pool):
    """A real pool's bootstrap queues EVERY fingerprint; one call served by B must leave ONLY B."""
    from agent import turn_usage
    serving = real_two_account_pool["kB-serving"]
    other = real_two_account_pool["kA-other"]
    agent = SimpleNamespace(provider=PROVIDER, base_url=SHARED_URL, api_key="kB-serving",
                            _credential_pool_entry_id=serving["entry_id"])
    pending = _drive_and_pending(lambda: turn_usage._mark_quota_dirty(agent))
    assert (PROVIDER, serving["fingerprint"]) in pending
    assert (PROVIDER, other["fingerprint"]) not in pending


def test_a_second_account_served_within_the_throttle_is_still_attributed(two_account_pool):
    """The provider dirty mark is throttled, but the per-account enqueue is not: an account served
    1s after another must still be attributed."""
    qs._last_touch.clear()
    with qs._pending_lock:
        qs._pending_refreshes.clear()
    qs.mark_called(PROVIDER, NOW, base_url=SHARED_URL, api_key="kB", entry_id="b")
    qs.mark_called(PROVIDER, NOW + timedelta(seconds=1), base_url=SHARED_URL, api_key="kA", entry_id="a")
    pending = qs.pending_refreshes()
    assert (PROVIDER, "sha256:bbb") in pending
    assert (PROVIDER, "sha256:aaa") in pending


def test_quota_aware_provider_writes_no_single_account_snapshot(qdir, quota_aware, monkeypatch):
    """A quota_aware provider must never publish a single account's usage as the whole
    subscription's: the legacy reader shows no confident number (D41)."""
    def one_account(provider):
        return AccountUsageSnapshot(
            provider=provider, source="t", fetched_at=NOW,
            windows=(AccountUsageWindow("Weekly", 100.0, NOW + timedelta(days=3),
                                        status="rate-limited"),))

    monkeypatch.setattr(qs, "default_fetcher", one_account)
    assert qs.refresh(PROVIDER, now=NOW) == "quota_aware"
    assert qs.read_snapshot(PROVIDER, NOW) is None
    groups = qs.status_groups(NOW, connected=lambda p: True, multi_account=lambda p: False)
    assert all(not g.get("windows") for g in groups)


def test_refresher_carries_pool_entries_into_the_facts_record(real_two_account_pool, monkeypatch):
    """The refresher path (not a hand-injected store) must carry the pool entries it saw, so a
    configured target can match and the labels reach the view - while the facts stay facts-only."""
    serving = real_two_account_pool["kB-serving"]

    def fake_for(provider, *, api_key=None, base_url=None):
        return AccountUsageSnapshot(
            provider=provider, source="t", fetched_at=NOW,
            windows=(AccountUsageWindow("Weekly", 100.0, NOW + timedelta(days=3),
                                        status="rate-limited"),))

    monkeypatch.setattr(qs, "default_fetcher_for", fake_for)
    monkeypatch.setattr(qs, "default_fetcher", lambda provider: fake_for(provider))
    qs.enqueue_refresh(PROVIDER, serving["fingerprint"])
    refresher = qs.QuotaRefresher(interval_s=0.05, jitter=0.0)
    refresher.start()
    try:
        deadline = time.monotonic() + 3
        while qs.read_account_facts(PROVIDER, serving["fingerprint"]) is None and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        refresher.stop()
    facts = qs.read_account_facts(PROVIDER, serving["fingerprint"])
    assert facts is not None and facts["entries"], "the refresher stored no pool entries"
    entry = facts["entries"][0]
    assert entry["entry_id"] == serving["entry_id"] and entry["label"]
    forbidden = {"metered", "state", "serving", "serving_since", "states", "history", "strategy", "read_health"}
    assert forbidden.isdisjoint(facts)
    view = qs.read_view("default", PROVIDER, now=NOW, policy={"metered_overflow": [entry["label"]]})
    acct = next(a for a in view["accounts"] if a["fingerprint"] == serving["fingerprint"])
    assert acct["metered"] is True
    assert acct["label"] == entry["label"]


def test_declared_metered_list_is_authoritative(qdir, quota_aware):
    """D49: a declared ``metered_overflow`` list decides metered-ness on its own; the provider's
    billing capability applies only when the key is not declared."""
    _store(fp="sha256:aaa", windows=[_facts_window(percent=100.0)],
           entries=[{"profile": "default", "entry_id": "a", "label": "FRESH_KEY"}])
    # declared list excludes this account -> NOT metered even though opencode-go bills
    excluded = qs.read_view("default", PROVIDER, now=NOW, policy={"metered_overflow": ["OTHER_KEY"]})
    assert (excluded["accounts"][0]["metered"], excluded["accounts"][0]["state"]) == (False, "exhausted")
    # an EMPTY declared list is still authoritative
    empty = qs.read_view("default", PROVIDER, now=NOW, policy={"metered_overflow": []})
    assert empty["accounts"][0]["metered"] is False
    # undeclared -> the provider capability decides
    undeclared = qs.read_view("default", PROVIDER, now=NOW, policy={})
    assert (undeclared["accounts"][0]["metered"], undeclared["accounts"][0]["state"]) == (True, "overflow")


def test_serving_follows_the_account_that_actually_served(two_account_pool):
    """``serving`` (and the published windows) must follow the attributed account, not a stale
    stored value."""
    _store(fp="sha256:aaa", windows=[_facts_window(percent=10.0)])
    _store(fp="sha256:bbb", windows=[_facts_window(percent=20.0)])
    qs.mark_called(PROVIDER, NOW, base_url=SHARED_URL, api_key="kB", entry_id="b")
    view = qs.read_view("default", PROVIDER, now=NOW, policy={})
    assert view["serving"] == "sha256:bbb"
    assert view["windows"] == qs.read_account_facts(PROVIDER, "sha256:bbb")["windows"]


def test_non_quota_aware_call_is_never_resolved_or_enqueued(qdir, monkeypatch):
    """A billed call on a fill_first provider must not resolve a pool, create facts or enqueue."""
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: False)
    resolved = []
    empty_pool = SimpleNamespace(entries=lambda: [], current=lambda: None)
    monkeypatch.setattr("agent.credential_pool.load_pool",
                        lambda provider, **kw: resolved.append(provider) or empty_pool)
    with qs._pending_lock:
        qs._pending_refreshes.clear()
    qs.mark_called("anthropic", NOW, base_url="https://api.anthropic.com")
    assert resolved == []
    assert qs.pending_refreshes() == set()
    facts_dir = qs.accounts_root() / "anthropic"
    assert not facts_dir.exists() or not list(facts_dir.glob("*.json"))


def test_refresher_is_reference_counted_per_pool(qdir, tmp_path, monkeypatch):
    """Stopping one live pool must not stop another's refreshing; the thread stops with the last."""
    monkeypatch.setattr("agent.credential_pool.get_pool_strategy", lambda provider: "quota_aware")
    monkeypatch.setattr(qs, "default_fetcher", lambda provider: AccountUsageSnapshot(
        provider=provider, source="t", fetched_at=NOW, windows=()))
    monkeypatch.setattr(qs, "default_fetcher_for", lambda provider, *, api_key=None, base_url=None:
                        AccountUsageSnapshot(provider=provider, source="t", fetched_at=NOW, windows=()))
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from agent.credential_pool import load_pool
    p1 = load_pool(PROVIDER)
    p2 = load_pool(PROVIDER)
    # Quiesce: a drain must not build a fresh pool (a drain must never re-queue work for itself).
    monkeypatch.setattr("agent.credential_pool.load_pool",
                        lambda provider, **kw: SimpleNamespace(entries=lambda: [], current=lambda: None))
    deadline = time.monotonic() + 3
    while qs.pending_refreshes() and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.4)  # let any in-flight drain finish so the refresher is idle before we stop it
    assert qs.is_refresher_running() is True
    p1.stop_quota_refresher()
    assert qs.is_refresher_running() is True
    p2.stop_quota_refresher()
    assert qs.is_refresher_running() is False


def test_agent_close_releases_the_quota_refresher(qdir, tmp_path, monkeypatch):
    """The real agent teardown (``close()``'s phase) releases the pool's hold on the refresher."""
    monkeypatch.setattr("agent.credential_pool.get_pool_strategy", lambda provider: "quota_aware")
    monkeypatch.setattr(qs, "default_fetcher", lambda provider: AccountUsageSnapshot(
        provider=provider, source="t", fetched_at=NOW, windows=()))
    monkeypatch.setattr(qs, "default_fetcher_for", lambda provider, *, api_key=None, base_url=None:
                        AccountUsageSnapshot(provider=provider, source="t", fetched_at=NOW, windows=()))
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from agent.credential_pool import load_pool
    from run_agent import AIAgent
    pool = load_pool(PROVIDER)
    monkeypatch.setattr("agent.credential_pool.load_pool",
                        lambda provider, **kw: SimpleNamespace(entries=lambda: [], current=lambda: None))
    deadline = time.monotonic() + 3
    while qs.pending_refreshes() and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.4)  # let any in-flight drain finish so the refresher is idle before we stop it
    assert qs.is_refresher_running() is True
    AIAgent._stop_quota_refresher(SimpleNamespace(_credential_pool=pool))
    assert qs.is_refresher_running() is False


def test_legacy_and_facts_paths_share_the_backoff_rule(qdir, monkeypatch):
    """One backoff helper is used by both the snapshot and the per-account facts paths."""
    seen = []
    real = qs._backoff_wait

    def spy(record, err):
        seen.append(err.status)
        return real(record, err)

    monkeypatch.setattr(qs, "_backoff_wait", spy)
    monkeypatch.setattr("agent.credential_pool.load_pool",
                        lambda provider, **kw: SimpleNamespace(entries=lambda: [], current=lambda: None))

    def boom():
        exc = RuntimeError("rate limited")
        exc.response = SimpleNamespace(status_code=429, headers={"Retry-After": "120"})
        return exc

    assert qs.refresh("anthropic", fetcher=lambda provider: (_ for _ in ()).throw(boom()), now=NOW) == "error"
    assert qs.fetch_account_facts(PROVIDER, "sha256:aaa",
                                  fetcher=lambda: (_ for _ in ()).throw(boom()), now=NOW) == "error"
    assert seen == [429, 429]


# ============================================================================
# Reader/profile scope, attribution, lifecycle and concurrency (D26, D33, D41, D42, D47)
# ============================================================================

def test_view_accounts_are_reader_scoped(qdir, quota_aware):
    """Reading profile beta sees ONLY beta's pool accounts, with beta's own entry identity and
    metered classification; alpha's account and alpha's alias are never borrowed (D33)."""
    _store(fp="sha256:shared", windows=[_facts_window(percent=100.0)], entries=[
        {"profile": "alpha", "entry_id": "alpha-id", "label": "CUSHION", "source": "env:CUSHION"},
        {"profile": "beta", "entry_id": "beta-id", "label": "NORMAL", "source": "env:NORMAL"}])
    _store(fp="sha256:alpha-only",
           entries=[{"profile": "alpha", "entry_id": "only", "label": "ONLY_ALPHA"}])
    view = qs.read_view("beta", PROVIDER, now=NOW, policy={"metered_overflow": ["CUSHION"]})
    shared = next(a for a in view["accounts"] if a["fingerprint"] == "sha256:shared")
    assert (shared["entry_id"], shared["label"], shared["metered"]) == ("beta-id", "NORMAL", False)
    assert not any(a["fingerprint"] == "sha256:alpha-only" for a in view["accounts"])


def test_read_view_uses_named_profile_config(qdir, tmp_path, monkeypatch):
    """read_view(profile, ...) resolves THAT profile's config for policy and strategy even when
    another profile is ambient (D23/D33)."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    homes = {}
    for name, targets in (("alpha", "[SPENT]"), ("beta", "[]")):
        home = tmp_path / ".hermes" / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(
            "credential_pool_strategies:\n  opencode-go: quota_aware\n"
            "credential_pool_policy:\n  opencode-go:\n    metered_overflow: " + targets + "\n")
        homes[name] = home
    _store(fp="sha256:shared", windows=[_facts_window(percent=100.0)],
           entries=[{"profile": n, "entry_id": n, "label": "SPENT"} for n in homes])
    monkeypatch.setenv("HERMES_HOME", str(homes["beta"]))
    a = qs.read_view("alpha", PROVIDER, now=NOW)
    b = qs.read_view("beta", PROVIDER, now=NOW)
    assert a["accounts"][0]["state"] == "overflow"
    assert b["accounts"][0]["state"] == "exhausted"


def test_refresher_carries_enqueue_profile_scope(qdir, tmp_path, monkeypatch):
    """A fingerprint queued by a NON-launch profile is fetched with THAT profile's home and
    credential, never resolved in the launch profile (D42)."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override, get_hermes_home
    from agent.secret_scope import set_secret_scope, reset_secret_scope, set_multiplex_active
    from agent.credential_pool import load_pool
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    home_a = tmp_path / ".hermes"
    home_b = home_a / "profiles" / "beta"
    home_b.mkdir(parents=True)
    home_a.joinpath("config.yaml").write_text("credential_pool_strategies:\n  opencode-go: fill_first\n")
    home_b.joinpath("config.yaml").write_text("credential_pool_strategies:\n  opencode-go: quota_aware\n")
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k-launch-A")
    monkeypatch.delenv("OPENCODE_GO_API_KEY_2", raising=False)
    seen = []

    def own(provider, *, api_key=None, base_url=None):
        seen.append((str(get_hermes_home()), api_key))
        return AccountUsageSnapshot(provider=provider, source="t", fetched_at=NOW, windows=())

    def fallback(provider):
        seen.append((str(get_hermes_home()), "DEFAULT_FALLBACK"))
        return AccountUsageSnapshot(provider=provider, source="t", fetched_at=NOW, windows=())

    monkeypatch.setattr(qs, "default_fetcher_for", own)
    monkeypatch.setattr(qs, "default_fetcher", fallback)
    monkeypatch.setattr(qs.QuotaRefresher, "_sleep_interval", lambda self: 0.05)
    token_home = set_hermes_home_override(home_b)
    token_scope = set_secret_scope({"OPENCODE_GO_API_KEY": "k-profile-B"})
    set_multiplex_active(True)
    try:
        load_pool(PROVIDER)
        pool = load_pool(PROVIDER)
        entries = pool.entries()
        assert len(entries) == 1 and entries[0].runtime_api_key == "k-profile-B"
        deadline = time.monotonic() + 3
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen, "the refresher never fetched the queued account"
        assert all(home == str(home_b) and key == "k-profile-B" for home, key in seen)
    finally:
        qs.stop_refresher()
        set_multiplex_active(False)
        reset_secret_scope(token_scope)
        reset_hermes_home_override(token_home)


def test_explicit_unmatched_key_never_attributes_single_account(qdir, tmp_path, monkeypatch):
    """An explicitly supplied credential matching no pool entry records NOTHING even when the
    pool holds exactly one account: the contract forbids guessing (D26/D42)."""
    from agent.credential_pool import load_pool
    from agent import auxiliary_client as aux
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("credential_pool_strategies:\n  opencode-go: quota_aware\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k-pool")
    monkeypatch.delenv("OPENCODE_GO_API_KEY_2", raising=False)
    load_pool(PROVIDER)
    pool = load_pool(PROVIDER)
    assert len(pool.entries()) == 1 and pool.entries()[0].runtime_api_key == "k-pool"
    client = _fake_aux_client(api_key="k-explicit-foreign", base_url=SHARED_URL)
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (client, "glm-5"))
    pending = _drive_and_pending(lambda: aux.call_llm(
        task="title_generation", provider=PROVIDER, model="glm-5",
        messages=[{"role": "user", "content": "hi"}]))
    assert pending == set()


def test_upgrade_does_not_serve_old_single_account_snapshot(qdir, monkeypatch):
    """A legacy single-account snapshot written before the provider became quota_aware is never
    published by status_groups/account_snapshot, and its bytes are preserved (D41)."""
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: False)
    snapshot = AccountUsageSnapshot(provider=PROVIDER, source="prior", fetched_at=NOW,
                                    windows=(AccountUsageWindow("Weekly", 99.0, NOW + timedelta(days=3)),))
    assert qs.refresh(PROVIDER, fetcher=lambda p: snapshot, connected=lambda p: True,
                      multi_account=lambda p: False, now=NOW) == "fetched"
    legacy = qdir / "snapshot" / f"{PROVIDER}.json"
    before = legacy.read_bytes()
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)
    assert qs.refresh(PROVIDER, now=NOW) == "quota_aware"
    assert legacy.read_bytes() == before
    groups = qs.status_groups(NOW, connected=lambda p: p == PROVIDER, multi_account=lambda p: False)
    account = qs.account_snapshot(PROVIDER, NOW)
    assert not any(g.get("windows") for g in groups)
    assert account is None or not account.windows


def test_concurrent_refresh_preserves_recorded_refusal(qdir, monkeypatch):
    """A refresh that read facts before a refusal was recorded must not overwrite the refusal:
    the per-fingerprint read-modify-write is serialized (D47)."""
    _store(fp="sha256:aaa", windows=[_facts_window(percent=100.0)])
    read_done = threading.Event()
    release = threading.Event()
    real_merge = qs._merge_facts_entries

    def pause_after_read(existing, entries):
        read_done.set()
        release.wait(5)
        return real_merge(existing, entries)

    monkeypatch.setattr(qs, "_merge_facts_entries", pause_after_read)
    results: dict = {}
    worker = threading.Thread(target=lambda: qs.store_account_facts(
        PROVIDER, "sha256:aaa", [_facts_window(percent=100.0)],
        fetched_at=NOW.isoformat(), now=NOW))
    refusal = threading.Thread(target=lambda: results.update(
        recorded=qs.record_refusal(PROVIDER, "sha256:aaa", state="overflow",
                                   reset_at=NOW + timedelta(hours=1), now=NOW)))
    worker.start()
    assert read_done.wait(3)          # the refresh has read facts, before any refusal exists
    refusal.start()
    # Wait until the refusal either lands (unserialized) or blocks on the shared record lock.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if qs.read_account_facts(PROVIDER, "sha256:aaa")["metered_exhausted_until"]:
            break
        time.sleep(0.02)
    release.set()
    worker.join(5)
    refusal.join(5)
    assert results.get("recorded") is True
    after = qs.read_account_facts(PROVIDER, "sha256:aaa")["metered_exhausted_until"]
    assert after == (NOW + timedelta(hours=1)).isoformat()


def test_refresher_stops_when_last_pool_is_collected(qdir, real_two_account_pool):
    """Collecting the last live pool (no explicit release) stops the refresher daemon: ownership
    is tied to live pools, so no orphan worker keeps fetching (D42)."""
    from agent.credential_pool import load_pool
    pool = load_pool(PROVIDER, quota_bootstrap=False)
    ref = weakref.ref(pool)
    assert qs.is_refresher_running()
    del pool
    deadline = time.monotonic() + 2
    while (ref() is not None or qs.is_refresher_running()) and time.monotonic() < deadline:
        gc.collect()
        time.sleep(0.02)
    assert ref() is None
    assert not qs._refresher_pools
    assert not qs.is_refresher_running()


def test_timed_out_stop_does_not_allow_second_worker(qdir, monkeypatch):
    """A stop that times out while a fetch is in flight must NOT let a replacement worker start:
    at most one quota-refresher thread exists (D42)."""
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(qs, "_pool_entry", lambda *a: None)

    def slow_fetch():
        entered.set()
        release.wait(6)
        return None

    qs.enqueue_refresh(PROVIDER, "sha256:aaa")
    old = qs.start_refresher(interval_s=0.05, fetcher=slow_fetch)
    assert entered.wait(3)
    old_thread = old._thread
    try:
        qs.stop_refresher()
        qs.start_refresher(interval_s=0.05, fetcher=lambda: None)
        alive = [t for t in threading.enumerate() if t.name == "quota-refresher"]
        assert len(alive) == 1
    finally:
        release.set()
        if old_thread is not None:
            old_thread.join(3)
        qs.stop_refresher()


def test_timed_out_stop_then_restart_keeps_a_live_worker(qdir, monkeypatch):
    """A stop that times out mid-fetch followed by a NEW owner must leave a RUNNING worker: the
    live owner holds the refresher, so the worker must resume (or be replaced) and keep draining
    the queue, never leave a live owner with no refresher (D42)."""
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(qs, "_pool_entry", lambda *a: None)

    def slow_fetch():
        entered.set()
        release.wait(6)
        return None

    class Owner:
        pass

    qs.enqueue_refresh(PROVIDER, "sha256:aaa")
    owner_a = Owner()
    old = qs.start_refresher(pool=owner_a, interval_s=0.05, fetcher=slow_fetch)
    assert entered.wait(3)
    try:
        qs.stop_refresher(pool=owner_a)          # last owner -> stop, join times out (2s)
        owner_b = Owner()
        qs.start_refresher(pool=owner_b, interval_s=0.05, fetcher=lambda: None)
        release.set()                            # the in-flight fetch now finishes
        time.sleep(0.5)
        assert len(qs._owner_tokens) == 1
        assert qs.is_refresher_running(), "a live owner holds the refresher but no worker runs"
    finally:
        release.set()
        qs.stop_refresher()


def test_release_vs_register_race_keeps_the_refresher_published(qdir, monkeypatch):
    """D42: the owner count and the worker lifecycle are serialized under one guard. When the
    last owner's release races a NEW owner's registration, the new owner's ``start()`` lands on
    the still-published refresher: the live owner keeps a running worker, a later owner reuses
    it (no second worker), and no orphan worker outlives every owner.

    Deterministic interleaving (no reliance on the scheduler): the last owner's ``stop()`` is
    held after it completes but before its guard re-check, while owner B is held after it grabs
    the refresher reference but before it calls ``start()``."""
    owner_a = _Owner()
    qs.start_refresher(pool=owner_a, interval_s=0.05, fetcher=lambda: None)
    refresher_obj = qs._refresher
    assert qs.is_refresher_running()

    a_stopped, a_go, b_parked, b_go = (threading.Event() for _ in range(4))
    real_stop, real_start = qs.QuotaRefresher.stop, qs.QuotaRefresher.start

    def gated_stop(self, timeout=2.0):
        result = real_stop(self, timeout)
        if threading.current_thread().name == "releaser":
            a_stopped.set()
            a_go.wait(5)              # hold AFTER stop() returns, BEFORE the guard re-check
        return result

    def gated_start(self):
        if threading.current_thread().name == "owner-b":
            b_parked.set()
            b_go.wait(5)              # hold AFTER grabbing the refresher ref, BEFORE start()
        return real_start(self)

    monkeypatch.setattr(qs.QuotaRefresher, "stop", gated_stop)
    monkeypatch.setattr(qs.QuotaRefresher, "start", gated_start)
    releaser = threading.Thread(target=lambda: qs.stop_refresher(pool=owner_a), name="releaser")
    releaser.start()
    owner_b_thread = None
    try:
        assert a_stopped.wait(5)
        owner_b = _Owner()
        owner_b_thread = threading.Thread(
            target=lambda: qs.start_refresher(pool=owner_b, interval_s=0.05, fetcher=lambda: None),
            name="owner-b")
        owner_b_thread.start()
        assert b_parked.wait(3)
        a_go.set()                    # let the last owner's release attempt its unpublish
        # A correct fix makes that release wait for B's start under the guard, so the refresher
        # stays published; the buggy version unpublishes it here.
        deadline = time.monotonic() + 0.5
        while qs._refresher is not None and time.monotonic() < deadline:
            time.sleep(0.005)
        published_after_a = qs._refresher
        b_go.set()
        owner_b_thread.join(5)
        releaser.join(5)
        assert published_after_a is refresher_obj, \
            "the last owner unpublished the refresher while a new owner was registering"
        assert qs._refresher is refresher_obj
        assert qs.is_refresher_running(), "a live owner was left with no running worker"
        assert len(qs._owner_tokens) == 1
        assert len(_live_refresher_threads()) == 1

        # A later owner must REUSE the published refresher, never build a second worker.
        owner_c = _Owner()
        qs.start_refresher(pool=owner_c, interval_s=0.05, fetcher=lambda: None)
        assert len(_live_refresher_threads()) == 1, "a second worker was built for one process"

        qs.stop_refresher(pool=owner_b)
        qs.stop_refresher(pool=owner_c)
        deadline = time.monotonic() + 2
        while _live_refresher_threads() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _live_refresher_threads() == [], "an orphan worker outlived every owner"
    finally:
        a_go.set()
        b_go.set()
        monkeypatch.setattr(qs.QuotaRefresher, "stop", real_stop)
        monkeypatch.setattr(qs.QuotaRefresher, "start", real_start)
        if refresher_obj is not None:
            refresher_obj.stop(1.0)   # reach any worker the race left unpublished
        qs.stop_refresher()
        if owner_b_thread is not None:
            owner_b_thread.join(2)
        releaser.join(2)
        for t in _live_refresher_threads():
            t.join(1)


def test_start_replaces_a_worker_committed_to_exit(qdir, monkeypatch):
    """D42: a worker that has already committed to leaving its loop (``_exiting``) must be
    REPLACED by a racing ``start()``, never revived. Reviving a leaving worker returns a
    refresher that dies as soon as that worker finishes, leaving a live owner with no worker."""
    exiting_reached, exit_go = threading.Event(), threading.Event()
    real_exit_pending = qs.QuotaRefresher._exit_pending

    def gated_exit_pending(self):
        leaving = real_exit_pending(self)
        if leaving and threading.current_thread().name == "quota-refresher":
            exiting_reached.set()
            exit_go.wait(5)           # stay alive, but committed to leaving
        return leaving

    refresher = qs.QuotaRefresher(interval_s=0.05, fetcher=lambda: None)
    refresher.start()
    old = refresher._thread
    try:
        assert old is not None and old.is_alive()
        monkeypatch.setattr(qs.QuotaRefresher, "_exit_pending", gated_exit_pending)
        # A stop() sets ``_stop``; the worker reaches ``_exit_pending`` and parks there with
        # ``_exiting`` already recorded, so it is alive yet on its way out.
        stopper = threading.Thread(target=lambda: refresher.stop(0.2), name="stopper")
        stopper.start()
        assert exiting_reached.wait(3)
        stopper.join(3)
        assert refresher._exiting is True and old.is_alive()
        # The racing start() must replace the leaving worker, not revive it.
        refresher.start()
        assert refresher._thread is not old, "a leaving worker was revived instead of replaced"
        assert refresher.running is True
        exit_go.set()
        old.join(3)
        assert not old.is_alive()
        assert refresher.running is True, "the live owner was left with no worker"
    finally:
        exit_go.set()
        monkeypatch.setattr(qs.QuotaRefresher, "_exit_pending", real_exit_pending)
        refresher.stop(1.0)
