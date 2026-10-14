"""Quota-aware credential SELECTION wiring, slice B2
(spec D3/D4/D10/D12/D21/D22/D27/D37/D39/D43/D44/D45/D47).

Behaviour contracts for the pure ``choose()``/ordering and every real selection entry point:
``CredentialPool.select`` (and ``_resolve_from_pool``), ``_acquire_lease_under_lock``, the
turn-boundary checkpoint, ``_revert_credential_rotation`` and the internal repair. Real
imports, a temp quota dir and a temp HERMES_HOME; nothing reads a source file's text.

Covers verification.md section 1 (pure ranking) and section 6 (every checkpoint), plus the
D28/D47 refusal -> selection integration and the D44 repair/pin rules.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent import quota_aware_selection as qsel
from agent import quota_aware_state as st
from agent import quota_state as qs
from agent.credential_pool import (STATUS_DEAD, STATUS_EXHAUSTED, CredentialPool,
                                    PooledCredential)

# The pool-level entry points under test (select / _quota_states / acquire_lease) read the real
# clock, so a frozen NOW makes every NOW-relative cooling deadline expire as wall time passes and
# the pool tests flip from pass to fail hours after they are written. Anchor the test clock to now.
NOW = datetime.now(timezone.utc).replace(microsecond=0)
PROVIDER = "opencode-go"
# Real opencode-go accounts share ONE base_url (spec.md Current State).
SHARED_URL = "https://opencode.ai/zen/go/v1"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def qdir(tmp_path, monkeypatch):
    """Temp quota dir with no real network/credential access."""
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
    # Clear the once-per-provider degradation warnings so a policy test starts clean.
    import agent.credential_pool as cp
    cp._POLICY_WARNED.clear()
    yield root
    qs.stop_refresher()
    with qs._pending_lock:
        qs._pending_refreshes.clear()
    cp._POLICY_WARNED.clear()


@pytest.fixture
def quota_aware(monkeypatch):
    """Force PROVIDER onto the ``quota_aware`` strategy without touching config."""
    monkeypatch.setattr("agent.credential_pool.get_pool_strategy",
                        lambda provider: "quota_aware" if provider == PROVIDER else "fill_first")
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)


# --------------------------------------------------------------------------- builders


def _win(*, period="7d", percent=0.0, reset_seconds=None, reset_days=None, status=None):
    reset = NOW + (timedelta(seconds=reset_seconds) if reset_seconds is not None
                   else timedelta(days=reset_days if reset_days is not None else 3))
    return {"period": period, "used_percent": percent, "status": status,
            "reset_at": reset.isoformat(), "window_seconds": 7 * 86400}


def _facts(fp, windows, *, entries=None, metered_exhausted_until=qs._UNSET, error=None,
           when=NOW, fetched=True):
    return qs.store_account_facts(
        PROVIDER, fp, windows,
        entries=entries or [{"profile": "default", "entry_id": fp[-3:], "label": fp[-3:]}],
        fetched_at=when.isoformat() if fetched else None,
        attempted_at=when.isoformat(), error=error,
        metered_exhausted_until=metered_exhausted_until, now=when,
    )


def _entry(entry_id, *, label=None, key=None, fingerprint=None, priority=0, base_url=SHARED_URL):
    e = PooledCredential(provider=PROVIDER, id=entry_id, label=label or entry_id,
                         auth_type="api_key", priority=priority, source=f"env:{label or entry_id}",
                         access_token=key or f"key-{entry_id}", base_url=base_url)
    e.extra["secret_fingerprint"] = fingerprint or f"sha256:{entry_id}"
    return e


def _pool(entries, *, strategy="quota_aware"):
    return CredentialPool(PROVIDER, list(entries))


class _Agent:
    """Minimal agent stand-in for the turn-boundary / revert checkpoints."""

    def __init__(self, pool, entry_id, *, base_url=SHARED_URL, model="glm-5",
                 provider=PROVIDER, revert_id=None):
        self._credential_pool = pool
        self._credential_pool_entry_id = entry_id
        self._credential_pool_revert_id = revert_id
        self.base_url = base_url
        self.model = model
        self.provider = provider
        self.swaps = []

    def _swap_credential(self, entry):
        self.swaps.append(entry.id)
        self._credential_pool_entry_id = entry.id
        return True


# ============================================================================
# 1. Pure ranking (D10/D21/D39/D47)
# ============================================================================


def _states(**by_id):
    """Build a states map from ``state`` / ``windows`` / ``recovery`` kwargs per entry id."""
    out = {}
    for eid, spec in by_id.items():
        out[eid] = qsel.SelectionState(
            state=spec.get("state", "unknown"),
            windows=tuple(spec.get("windows", ())),
            effective_recovery=spec.get("recovery"),
        )
    return out


def test_total_order_classes():
    entries = [_entry("p1", priority=1), _entry("p0", priority=0),
               _entry("ov"), _entry("ex")]
    states = _states(p1={"state": "covered"}, p0={"state": "covered"},
                     ov={"state": "overflow"}, ex={"state": "exhausted"})
    ordered = [e.id for e in qsel.ordered_candidates(entries, states, qsel.Policy(), NOW)]
    assert ordered == ["p0", "p1", "ov", "ex"]


def test_priority_wins_over_drain_reset():
    """A(priority 0, reset 300) vs B(priority 1, reset 100) -> A first (D39)."""
    a = _entry("A", priority=0)
    b = _entry("B", priority=1)
    states = _states(
        A={"state": "covered", "windows": [_win(percent=50.0, reset_seconds=300)]},
        B={"state": "covered", "windows": [_win(percent=50.0, reset_seconds=100)]},
    )
    ordered = [e.id for e in qsel.ordered_candidates([a, b], states, qsel.Policy(), NOW)]
    assert ordered == ["A", "B"]


def test_same_priority_drains_sooner_reset_first():
    a = _entry("A", priority=0)
    b = _entry("B", priority=0)
    states = _states(
        A={"state": "covered", "windows": [_win(percent=50.0, reset_seconds=300)]},
        B={"state": "covered", "windows": [_win(percent=50.0, reset_seconds=100)]},
    )
    ordered = [e.id for e in qsel.ordered_candidates([a, b], states, qsel.Policy(), NOW)]
    assert ordered == ["B", "A"]  # sooner reset drained first within one priority


def test_earliest_reset_first_false_drops_the_reset_term():
    a = _entry("A", priority=0)
    b = _entry("B", priority=0)
    states = _states(
        A={"state": "covered", "windows": [_win(percent=50.0, reset_seconds=300)]},
        B={"state": "covered", "windows": [_win(percent=50.0, reset_seconds=100)]},
    )
    ordered = [e.id for e in qsel.ordered_candidates([a, b], states, qsel.Policy(earliest_reset_first=False), NOW)]
    assert ordered == ["A", "B"]  # stable input order; no reset term at all


def test_covered_beats_unknown():
    c = _entry("C")
    u = _entry("U")
    states = _states(C={"state": "covered"}, U={"state": "unknown"})
    ordered = [e.id for e in qsel.ordered_candidates([u, c], states, qsel.Policy(), NOW)]
    assert ordered == ["C", "U"]


def test_exhausted_order_by_effective_recovery_then_priority():
    a = _entry("A", priority=5)
    b = _entry("B", priority=0)
    c = _entry("C", priority=1)
    states = _states(
        A={"state": "exhausted", "recovery": NOW + timedelta(seconds=50)},
        B={"state": "exhausted", "recovery": NOW + timedelta(seconds=200)},
        C={"state": "exhausted", "recovery": NOW + timedelta(seconds=50)},
    )
    ordered = [e.id for e in qsel.ordered_candidates([a, b, c], states, qsel.Policy(), NOW)]
    # sooner recovery first; tie between A(prio5) and C(prio1) -> priority breaks it.
    assert ordered == ["C", "A", "B"]


def test_covered_tiebreak_ignores_recovery_and_is_constant():
    """Inside one priority the drain key is the most-constrained window's reset; a covered
    account carries no recovery term, so a wild ``effective_recovery`` cannot reorder it."""
    a = _entry("A", priority=0)
    b = _entry("B", priority=0)
    states = _states(
        A={"state": "covered", "windows": [_win(percent=80.0, reset_seconds=100)],
           "recovery": NOW + timedelta(days=99)},
        B={"state": "covered", "windows": [_win(percent=10.0, reset_seconds=50)],
           "recovery": NOW + timedelta(days=1)},
    )
    first = [e.id for e in qsel.ordered_candidates([a, b], states, qsel.Policy(), NOW)]
    # A later clock leaves the covered keys unchanged (no recovery term).
    second = [e.id for e in qsel.ordered_candidates([a, b], states, qsel.Policy(), NOW + timedelta(hours=1))]
    assert first == second == ["B", "A"]  # B's binding window resets sooner -> drained first


def test_n3_mixed_no_n2_assumption():
    entries = [_entry("a"), _entry("b"), _entry("c")]
    states = _states(a={"state": "overflow"}, b={"state": "covered"}, c={"state": "covered"})
    ordered = [e.id for e in qsel.ordered_candidates(entries, states, qsel.Policy(), NOW)]
    assert ordered[0] == "b" and ordered[-1] == "a" and len(ordered) == 3


def test_turn_boundary_keeps_current_covered_even_when_a_sibling_becomes_covered():
    a = _entry("A", priority=1)
    b = _entry("B", priority=0)
    states = _states(A={"state": "covered"}, B={"state": "covered"})
    chosen = qsel.choose([a, b], states, qsel.Policy(), "A", qsel.MODE_TURN_BOUNDARY, NOW)
    assert chosen.id == "A"  # sticky; a higher-priority sibling does not move a live session


def test_overflow_current_moves_to_a_covered_sibling():
    a = _entry("A", priority=0)
    b = _entry("B", priority=1)
    states = _states(A={"state": "overflow"}, B={"state": "covered"})
    chosen = qsel.choose([a, b], states, qsel.Policy(), "A", qsel.MODE_TURN_BOUNDARY, NOW)
    assert chosen.id == "B"


def test_unknown_current_stays():
    a = _entry("A")
    b = _entry("B")
    states = _states(A={"state": "unknown"}, B={"state": "covered"})
    chosen = qsel.choose([a, b], states, qsel.Policy(), "A", qsel.MODE_TURN_BOUNDARY, NOW)
    assert chosen.id == "A"  # fail-open sticky


def test_session_start_ignores_current_binding():
    a = _entry("A", priority=1)
    b = _entry("B", priority=0)
    states = _states(A={"state": "covered"}, B={"state": "covered"})
    chosen = qsel.choose([a, b], states, qsel.Policy(), "A", qsel.MODE_SESSION_START, NOW)
    assert chosen.id == "B"  # initial assignment takes the head of the order


# ============================================================================
# 6. Every checkpoint (D22/D27/D37/D43/D44)
# ============================================================================


def test_select_prefers_covered_over_metered(qdir, quota_aware):
    """A covered account is never passed over for a metered one."""
    _facts("sha256:aaa", [_win(percent=10.0)])
    _facts("sha256:bbb", [_win(percent=100.0)])  # plan spent -> overflow (metered)
    pool = _pool([_entry("a", fingerprint="sha256:aaa"), _entry("b", fingerprint="sha256:bbb")])
    assert pool.select().id == "a"


def test_select_metered_strictly_last(qdir, quota_aware):
    """Nothing covered: a usable metered account outranks a cushion-refused one (D17/D47)."""
    _facts("sha256:aaa", [_win(percent=100.0)])  # overflow (metered)
    _facts("sha256:bbb", [_win(percent=100.0)],
           metered_exhausted_until=NOW + timedelta(hours=1))  # exhausted (refusal)
    pool = _pool([_entry("a", fingerprint="sha256:aaa"), _entry("b", fingerprint="sha256:bbb")])
    assert pool.select().id == "a"


def test_select_is_sticky_within_the_same_account(qdir, quota_aware):
    """A live session keeps its covered account; a counter/sibling never rebinds it."""
    _facts("sha256:aaa", [_win(percent=10.0)])
    _facts("sha256:bbb", [_win(percent=20.0)])
    pool = _pool([_entry("a", priority=1, fingerprint="sha256:aaa"),
                  _entry("b", priority=0, fingerprint="sha256:bbb")])
    first = pool.select()
    assert first.id == "b"  # initial: priority 0
    # B stays covered -> every later select keeps B even though A now looks better on reset.
    assert pool.select().id == "b"
    assert pool.select().id == "b"


def test_turn_boundary_switches_overflow_to_covered(qdir, quota_aware, caplog):
    """Headline D22 case: a session silently overflowing moves at the next turn boundary."""
    from agent.agent_runtime_helpers import quota_aware_turn_boundary
    _facts("sha256:aaa", [_win(percent=100.0)])  # A overflow (billing)
    _facts("sha256:bbb", [_win(percent=5.0)])    # B covered
    pool = _pool([_entry("a", fingerprint="sha256:aaa"), _entry("b", fingerprint="sha256:bbb")])
    agent = _Agent(pool, "a")
    with caplog.at_level(logging.INFO):
        quota_aware_turn_boundary(agent)
    assert agent.swaps == ["b"]
    assert agent._credential_pool_entry_id == "b"
    assert any("turn boundary" in r.message.lower() for r in caplog.records)


def test_turn_boundary_keeps_a_covered_session(qdir, quota_aware):
    from agent.agent_runtime_helpers import quota_aware_turn_boundary
    _facts("sha256:aaa", [_win(percent=10.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_entry("a", priority=1, fingerprint="sha256:aaa"),
                  _entry("b", priority=0, fingerprint="sha256:bbb")])
    agent = _Agent(pool, "a")
    quota_aware_turn_boundary(agent)
    assert agent.swaps == []  # sticky while covered


def test_D43_initial_spread_but_live_session_not_rebound(qdir, quota_aware):
    """A new child run may spread to a free covered account; a live session is never rebound."""
    _facts("sha256:aaa", [_win(percent=10.0)])
    _facts("sha256:bbb", [_win(percent=10.0)])
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa"),
                  _entry("b", priority=1, fingerprint="sha256:bbb")])
    assert pool.acquire_lease("a") == "a"      # A holds a lease (cap default 1)
    assert pool.acquire_lease() == "b"         # initial assignment spreads to the free covered B
    # A live session on A stays on A even though B is idle.
    chosen = pool.quota_choose(current_id="a", mode="turn_boundary")
    assert chosen.id == "a"


def test_D37_covered_at_cap_never_descends_then_a_real_refusal_moves_it(qdir, quota_aware):
    """A covered account at its cap must not push work to an idle overflow account (D37)."""
    _facts("sha256:aaa", [_win(percent=10.0)])   # A covered
    _facts("sha256:bbb", [_win(percent=100.0)])  # B overflow (metered, idle)
    pool = _pool([_entry("a", fingerprint="sha256:aaa"), _entry("b", fingerprint="sha256:bbb")])
    assert pool.acquire_lease("a") == "a"          # A at its cap (default 1)
    assert pool.acquire_lease() == "a"             # overcommit A; never descend to B
    # A real refusal (429) DOES move it.
    entry = _entry("a", fingerprint="sha256:aaa", key="key-a")
    nxt = pool.mark_exhausted_and_rotate(status_code=429, api_key_hint="key-a", credential_id="a")
    assert nxt is not None and nxt.id == "b"


def test_quota_summary_reports_has_covered(qdir, quota_aware):
    _facts("sha256:aaa", [_win(percent=10.0)])
    _facts("sha256:bbb", [_win(percent=100.0)])
    pool = _pool([_entry("a", fingerprint="sha256:aaa"), _entry("b", fingerprint="sha256:bbb")])
    summary = pool.quota_summary()
    assert summary["has_covered"] is True
    assert summary["counts"]["covered"] == 1 and summary["counts"]["overflow"] == 1
    # nothing covered -> has_covered False
    _facts("sha256:aaa", [_win(percent=100.0)])
    pool2 = _pool([_entry("a", fingerprint="sha256:aaa")])
    assert pool2.quota_summary()["has_covered"] is False


def test_has_available_semantics_unchanged(qdir, quota_aware):
    """D27: has_available stays cooldown/dead based so the D17 handout still reaches rotation."""
    _facts("sha256:aaa", [_win(percent=100.0)])
    pool = _pool([_entry("a", fingerprint="sha256:aaa")])
    assert pool.has_available() is True  # a quota-spent account is still handed out
    assert pool.select() is not None


def test_no_fetch_on_the_selection_path(qdir, quota_aware, monkeypatch):
    monkeypatch.setattr(qs, "fetch_account_facts",
                        lambda *a, **k: pytest.fail("fetch on a request path"))
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_entry("a", fingerprint="sha256:aaa"), _entry("b", fingerprint="sha256:bbb")])
    pool.select()
    pool.acquire_lease()
    pool.quota_choose(current_id="a", mode="turn_boundary")


# ---- D28/D47 refusal recording integrated with selection -------------------


def test_D28_D47_refusal_while_overflow_removes_it_from_selection(qdir, quota_aware):
    """A cushion refusal recorded while A served as ``overflow`` makes A ``exhausted`` and it
    drops behind a covered sibling (D28/D47)."""
    _facts("sha256:aaa", [_win(percent=100.0)])   # A overflow (serving as cushion)
    _facts("sha256:bbb", [_win(percent=5.0)])     # B covered
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa"),
                  _entry("b", priority=1, fingerprint="sha256:bbb")])
    # A outranks B by priority, but B is the covered account.
    assert pool.select().id == "b"
    assert qs.record_refusal(PROVIDER, "sha256:aaa", state="overflow",
                             reset_at=NOW + timedelta(hours=2), now=NOW) is True
    # A is now exhausted; selection still returns covered B.
    assert pool.select().id == "b"
    states = pool._quota_states(pool.entries())
    assert states["a"].state == "exhausted"


def test_refusal_while_covered_records_nothing(qdir, quota_aware):
    _facts("sha256:aaa", [_win(percent=10.0)])
    assert qs.record_refusal(PROVIDER, "sha256:aaa", state="covered",
                             reset_at=NOW + timedelta(hours=2), now=NOW) is False
    assert qs.read_account_facts(PROVIDER, "sha256:aaa")["metered_exhausted_until"] is None


# ---- D44 explicit pins vs internal repair ---------------------------------


def test_D44_operator_pin_is_honoured_even_when_not_covered(qdir, quota_aware):
    _facts("sha256:aaa", [_win(percent=10.0)])   # A covered
    _facts("sha256:bbb", [_win(percent=100.0)])  # B overflow (not covered)
    pool = _pool([_entry("a", fingerprint="sha256:aaa"), _entry("b", fingerprint="sha256:bbb")])
    assert pool.acquire_lease("b", pin=True) == "b"


def test_D44_internal_explicit_id_is_validated(qdir, quota_aware):
    """An internal caller may not bypass eligibility: a cooled/dead id falls back to order."""
    _facts("sha256:aaa", [_win(percent=10.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    a = _entry("a", priority=0, fingerprint="sha256:aaa")
    b = _entry("b", priority=1, fingerprint="sha256:bbb")
    b.last_status = "dead"
    pool = _pool([a, b])
    # Internal lease of the dead entry is refused; the ordered pick returns the live one.
    assert pool.acquire_lease("b") == "a"


def test_D44_internal_repair_orders_by_quota_not_first_match(qdir, quota_aware):
    """The delegate repair picks the covered endpoint-matching entry, not the first non-dead."""
    from tools.delegate_tool_child_run import _lease_child_credential
    _facts("sha256:aaa", [_win(percent=100.0)])  # A overflow
    _facts("sha256:bbb", [_win(percent=5.0)])    # B covered
    # A sorts first by priority but is not covered; B must win.
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa"),
                  _entry("b", priority=1, fingerprint="sha256:bbb")])
    child = SimpleNamespace(provider=PROVIDER, base_url=SHARED_URL, _credential_pool=pool)
    bound = []
    child._swap_credential = lambda e: bound.append(e.id) or True
    _pool_out, lease_id = _lease_child_credential(child)
    assert lease_id == "b"
    assert bound == ["b"]


def test_D44_repair_with_no_endpoint_match_falls_to_D17_and_logs(qdir, quota_aware, caplog):
    from tools.delegate_tool_child_run import _lease_child_credential
    _facts("sha256:aaa", [_win(percent=5.0)])
    other_url = "https://elsewhere.example/zen/go/v1"
    pool = _pool([_entry("a", fingerprint="sha256:aaa", base_url=other_url)])
    child = SimpleNamespace(provider=PROVIDER, base_url=SHARED_URL, _credential_pool=pool)
    child._swap_credential = lambda e: True
    with caplog.at_level(logging.INFO):
        _pool_out, lease_id = _lease_child_credential(child)
    assert lease_id == "a"  # D17 handout
    assert any("D17" in r.message for r in caplog.records)


# ---- rotation / revert -----------------------------------------------------


def test_revert_does_not_land_on_a_non_covered_entry(qdir, quota_aware):
    """D22/D43: the revert may not pull a covered live session back onto a non-covered entry."""
    from agent.agent_runtime_helpers import _revert_credential_rotation
    _facts("sha256:aaa", [_win(percent=10.0)])   # current A covered
    _facts("sha256:bbb", [_win(percent=100.0)])  # revert target B overflow
    pool = _pool([_entry("a", fingerprint="sha256:aaa"), _entry("b", fingerprint="sha256:bbb")])
    agent = _Agent(pool, "a", revert_id="b")
    _revert_credential_rotation(agent)
    assert agent.swaps == []                       # no revert
    assert agent._credential_pool_revert_id is None


def test_revert_lands_on_a_covered_target(qdir, quota_aware):
    from agent.agent_runtime_helpers import _revert_credential_rotation
    _facts("sha256:aaa", [_win(percent=100.0)])  # current A overflow
    _facts("sha256:bbb", [_win(percent=5.0)])    # revert target B covered
    pool = _pool([_entry("a", fingerprint="sha256:aaa"), _entry("b", fingerprint="sha256:bbb")])
    agent = _Agent(pool, "a", revert_id="b")
    _revert_credential_rotation(agent)
    assert agent.swaps == ["b"]


def test_resolve_from_pool_returns_the_choose_result(qdir, quota_aware, monkeypatch):
    """``_resolve_from_pool`` routes through the quota-aware select() (D22/D41)."""
    from hermes_cli import runtime_provider as rp
    _facts("sha256:aaa", [_win(percent=100.0)])  # A overflow
    _facts("sha256:bbb", [_win(percent=5.0)])    # B covered
    pool = _pool([_entry("a", fingerprint="sha256:aaa"), _entry("b", fingerprint="sha256:bbb")])
    monkeypatch.setattr(rp, "load_pool", lambda provider: pool)
    rt = rp._resolve_from_pool(PROVIDER, PROVIDER, {}, None, None, None)
    assert rt is not None
    assert rt["api_key"] == "key-b"  # the covered account, same as choose()


# ============================================================================
# 7 (B2 half). Capability and policy degradation (D15/D40/D45)
# ============================================================================


def test_provider_without_capability_degrades_to_fill_first(qdir, monkeypatch, caplog):
    """v1 supports opencode-go only: any other provider orders as fill_first and logs once."""
    from agent.credential_pool import CredentialPool, resolve_selection_policy
    monkeypatch.setattr("agent.credential_pool.get_pool_strategy", lambda provider: "quota_aware")
    _facts("sha256:aaa", [_win(percent=100.0)], )  # spent, but provider has no capability
    e = PooledCredential(provider="anthropic", id="a", label="A", auth_type="api_key",
                         priority=0, source="env:A", access_token="k")
    e.extra["secret_fingerprint"] = "sha256:aaa"
    with caplog.at_level(logging.WARNING):
        policy, usable = resolve_selection_policy("anthropic", [e])
    assert usable is False
    assert any("degraded to fill_first" in r.message for r in caplog.records)


def test_malformed_metered_overflow_degrades_to_fill_first(qdir, quota_aware, monkeypatch, caplog):
    import agent.credential_pool as cp
    monkeypatch.setattr(cp, "get_pool_policy_block",
                        lambda provider: {"metered_overflow": "not-a-list"})
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa"),
                  _entry("b", priority=1, fingerprint="sha256:bbb")])
    with caplog.at_level(logging.WARNING):
        assert pool.select().id == "a"  # fill_first: priority order, not covered-first
    assert any("degraded to fill_first" in r.message for r in caplog.records)


def test_unresolved_policy_target_degrades_to_fill_first(qdir, quota_aware, monkeypatch, caplog):
    import agent.credential_pool as cp
    monkeypatch.setattr(cp, "get_pool_policy_block",
                        lambda provider: {"metered_overflow": ["NO_SUCH_ACCOUNT"]})
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa"),
                  _entry("b", priority=1, fingerprint="sha256:bbb")])
    with caplog.at_level(logging.WARNING):
        assert pool.select().id == "a"
    assert any("degraded to fill_first" in r.message for r in caplog.records)


def test_configured_metered_overflow_target_resolves(qdir, quota_aware, monkeypatch):
    import agent.credential_pool as cp
    monkeypatch.setattr(cp, "get_pool_policy_block",
                        lambda provider: {"metered_overflow": ["OPENCODE_GO_API_KEY"]})
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa", label="OPENCODE_GO_API_KEY_2"),
                  _entry("b", priority=1, fingerprint="sha256:bbb", label="OPENCODE_GO_API_KEY")])
    assert pool.select().id == "b"  # policy usable -> covered-first ordering


# ============================================================================
# 8. D46(a). "configuration did not take effect" metered warning (once per load)
# ============================================================================


def _d46a_records(caplog):
    return [r for r in caplog.records if "configuration did not take effect" in r.message]


def test_D46a_fires_once_when_configured_target_matches_no_account(
    qdir, quota_aware, monkeypatch, caplog, tmp_path,
):
    """Case 1: a billing-capable provider (opencode-go) whose configured ``metered_overflow``
    list names no account -> WARN once per load, naming the account and the targets (D46a)."""
    import agent.credential_pool as cp
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cp, "get_pool_policy_block",
                        lambda provider: {"metered_overflow": ["NO_SUCH_ACCOUNT"]})
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa", label="OPENCODE_GO_API_KEY_2"),
                  _entry("b", priority=1, fingerprint="sha256:bbb", label="OPENCODE_GO_API_KEY")])
    with caplog.at_level(logging.WARNING):
        pool.select()
        pool.select()
    recs = _d46a_records(caplog)
    assert len(recs) == 1
    msg = recs[0].message
    assert "NO_SUCH_ACCOUNT" in msg                                  # the configured targets
    assert "OPENCODE_GO_API_KEY_2" in msg and "OPENCODE_GO_API_KEY" in msg  # the account(s)
    assert "opencode-go" in msg


def test_D46a_fires_once_when_no_metered_overflow_configured(
    qdir, quota_aware, monkeypatch, caplog, tmp_path,
):
    """Case 2: no ``metered_overflow`` at all on a billing-capable provider -> WARN once."""
    import agent.credential_pool as cp
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cp, "get_pool_policy_block", lambda provider: {})
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)])
    pool = _pool([_entry("a", fingerprint="sha256:aaa", label="OPENCODE_GO_API_KEY")])
    with caplog.at_level(logging.WARNING):
        pool.select()
        pool.select()
    recs = _d46a_records(caplog)
    assert len(recs) == 1
    assert "not configured" in recs[0].message
    assert "OPENCODE_GO_API_KEY" in recs[0].message


def test_D46a_silent_when_configured_target_resolves(
    qdir, quota_aware, monkeypatch, caplog, tmp_path,
):
    """Case 3: a correctly configured list names an account -> (a) does NOT fire."""
    import agent.credential_pool as cp
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cp, "get_pool_policy_block",
                        lambda provider: {"metered_overflow": ["OPENCODE_GO_API_KEY"]})
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa", label="OPENCODE_GO_API_KEY_2"),
                  _entry("b", priority=1, fingerprint="sha256:bbb", label="OPENCODE_GO_API_KEY")])
    with caplog.at_level(logging.WARNING):
        pool.select()
    assert _d46a_records(caplog) == []


def test_D46a_does_not_repeat_across_selections_in_one_load(
    qdir, quota_aware, monkeypatch, caplog, tmp_path,
):
    """D46's last sentence: (a) is per load, never per selection."""
    import agent.credential_pool as cp
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cp, "get_pool_policy_block", lambda provider: {})
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa"),
                  _entry("b", priority=1, fingerprint="sha256:bbb")])
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            pool.select()
            pool.quota_choose()
            pool.order_entries(list(pool.entries()))
    assert len(_d46a_records(caplog)) == 1


def test_D46a_same_ineffective_config_warns_once_across_loads(
    qdir, quota_aware, monkeypatch, caplog, tmp_path,
):
    """A *stable* ineffective configuration keeps to one warning: several selections, each a
    fresh load of the same ``credential_pool_policy``, never re-fire it (D46a per load)."""
    import agent.credential_pool as cp
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cp, "get_pool_policy_block",
                        lambda provider: {"metered_overflow": ["NO_SUCH_ACCOUNT"]})
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa"),
                  _entry("b", priority=1, fingerprint="sha256:bbb")])
    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            pool.select()
            pool.quota_choose()
            pool.order_entries(list(pool.entries()))
    assert len(_d46a_records(caplog)) == 1


def test_D46a_warns_again_after_the_configuration_changes(
    qdir, quota_aware, monkeypatch, caplog, tmp_path,
):
    """D46(a) is per *effective* configuration: the same ineffective block warns once, but
    editing ``credential_pool_policy`` to another still-unmatched target warns again on the
    next load — so a long-lived process cannot go silent after a config edit (no silent
    billing)."""
    import agent.credential_pool as cp
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    block = {"metered_overflow": ["MISSING_A"]}
    monkeypatch.setattr(cp, "get_pool_policy_block", lambda provider: block)
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa"),
                  _entry("b", priority=1, fingerprint="sha256:bbb")])
    with caplog.at_level(logging.WARNING):
        pool.select()
        pool.select()                                   # same config -> still one warning
        assert len(_d46a_records(caplog)) == 1
        block["metered_overflow"] = ["MISSING_B"]       # the user edits the config
        pool.select()                                   # next load -> warns again
    recs = _d46a_records(caplog)
    assert len(recs) == 2
    assert "MISSING_A" in recs[0].message
    assert "MISSING_B" in recs[1].message


def test_D46a_silent_for_a_correctly_configured_list(
    qdir, quota_aware, monkeypatch, caplog, tmp_path,
):
    """(3) A correctly configured ``metered_overflow`` that names its account never warns — and
    once a user FIXES a previously ineffective config, the warning stops too (no noise)."""
    import agent.credential_pool as cp
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    block = {"metered_overflow": ["OPENCODE_GO_API_KEY"]}
    monkeypatch.setattr(cp, "get_pool_policy_block", lambda provider: block)
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa", label="OPENCODE_GO_API_KEY_2"),
                  _entry("b", priority=1, fingerprint="sha256:bbb", label="OPENCODE_GO_API_KEY")])
    with caplog.at_level(logging.WARNING):
        pool.select()
        pool.select()
        assert _d46a_records(caplog) == []              # a good config is silent
        block["metered_overflow"] = ["NO_SUCH_ACCOUNT"]  # becomes ineffective
        pool.select()
        assert len(_d46a_records(caplog)) == 1          # ...then it warns once
        block["metered_overflow"] = ["OPENCODE_GO_API_KEY"]  # the user fixes it
        pool.select()
    assert len(_d46a_records(caplog)) == 1              # ...and a fix adds no further noise


def test_missing_earliest_reset_first_defaults_true(qdir, quota_aware, monkeypatch, tmp_path):
    """D39/D5: a policy block that omits ``earliest_reset_first`` resolves to the documented
    default (true) and keeps the same-priority drain tie-break."""
    import agent.credential_pool as cp
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cp, "get_pool_policy_block",
                        lambda provider: {"metered_overflow": ["OPENCODE_GO_API_KEY"]})
    cp._POLICY_WARNED.clear()
    assert qsel.EARLIEST_RESET_FIRST_DEFAULT is True
    entries = [_entry("a", label="OPENCODE_GO_API_KEY")]
    policy, usable = cp.resolve_selection_policy(PROVIDER, entries)
    assert usable is True
    assert policy.earliest_reset_first is True
    # ...and the default actually engages the drain tie-break within one priority (D39).
    a = _entry("A", priority=0)
    b = _entry("B", priority=0)
    states = _states(
        A={"state": "covered", "windows": [_win(percent=50.0, reset_seconds=300)]},
        B={"state": "covered", "windows": [_win(percent=50.0, reset_seconds=100)]},
    )
    ordered = [e.id for e in qsel.ordered_candidates([a, b], states, policy, NOW)]
    assert ordered == ["B", "A"]


# ============================================================================
# 9. Endpoint repair eligibility, turn-boundary endpoint siblings, the
#    turn-context/aux credential agreement and the D45 target forms.
# ============================================================================


def test_D44_repair_never_binds_a_cooling_or_non_leased_account(qdir, quota_aware):
    """D44: the internal repair restricts the endpoint candidates to ELIGIBLE entries and
    re-resolves the bound object from the lease the pool RETURNS. A cooling endpoint-mate (B)
    is never repaired onto, and the leased account always equals the serving account."""
    from tools.delegate_tool_child_run import _lease_child_credential
    for fp, pct in [("sha256:aaa", 5), ("sha256:bbb", 5), ("sha256:ccc", 100)]:
        _facts(fp, [_win(percent=pct)])
    a = _entry("a", fingerprint="sha256:aaa", base_url="https://elsewhere.example/v1")
    b = _entry("b", fingerprint="sha256:bbb")            # serves the endpoint but is cooling
    b.last_status = "exhausted"
    b.last_status_at = time.time()
    b.last_error_reset_at = time.time() + 3600
    c = _entry("c", fingerprint="sha256:ccc")            # the eligible endpoint-mate
    pool = _pool([a, b, c])
    bound = []
    child = SimpleNamespace(provider=PROVIDER, base_url=SHARED_URL, _credential_pool=pool,
                            _swap_credential=lambda e: bound.append(e.id) or True)
    _pool_out, lease = _lease_child_credential(child)
    assert lease == bound[-1] == "c"                     # never the cooling B; bound == leased
    assert pool._active_leases.get("b", 0) == 0          # the cooling account is not leased


def test_turn_boundary_finds_an_endpoint_matching_covered_sibling(qdir, quota_aware):
    """D22: one endpoint-incompatible order head must not abort the whole check — a covered
    sibling that serves the session's endpoint is still found and switched to."""
    from agent.agent_runtime_helpers import quota_aware_turn_boundary
    _facts("sha256:aaa", [_win(percent=100.0)])          # A overflow (the live binding)
    _facts("sha256:bbb", [_win(percent=5.0)])            # B covered, ANOTHER endpoint
    _facts("sha256:ccc", [_win(percent=5.0)])            # C covered, the SAME endpoint
    pool = _pool([_entry("a", fingerprint="sha256:aaa"),
                  _entry("b", fingerprint="sha256:bbb", base_url="https://elsewhere.example/v1"),
                  _entry("c", fingerprint="sha256:ccc", priority=1)])
    agent = _Agent(pool, "a")
    quota_aware_turn_boundary(agent)
    assert agent.swaps == ["c"]
    assert agent._credential_pool_entry_id == "c"


def test_turn_context_publishes_the_post_boundary_credential_to_auxiliary(qdir, quota_aware):
    """D22/sibling-path: the runtime main is published AFTER the quota-aware turn-boundary
    switch, so the auxiliary routing pins the SAME final credential the main request uses —
    compression/title calls must not keep billing the overflow account the main left."""
    from tests.agent.test_turn_context import _FakeAgent, _build
    from agent import auxiliary_client as aux
    _facts("sha256:aaa", [_win(percent=100.0)])          # A overflow (the live binding)
    _facts("sha256:bbb", [_win(percent=5.0)])            # B covered
    pool = _pool([_entry("a", fingerprint="sha256:aaa"), _entry("b", fingerprint="sha256:bbb")])
    agent = _FakeAgent()
    agent.provider = agent.requested_provider = PROVIDER
    agent.model = "glm-5"
    agent.base_url = SHARED_URL
    agent.api_key = "key-a"
    agent._credential_pool = pool
    agent._credential_pool_entry_id = "a"

    def _swap(entry):
        agent._credential_pool_entry_id = entry.id
        agent.api_key = entry.access_token
        return True

    agent._swap_credential = _swap
    try:
        _build(agent)
        client, _model, _provider = aux._resolve_auto_route(task="compression")
        try:
            assert agent.api_key == "key-b"
            assert aux._runtime_main_value("api_key") == "key-b"
            assert client.api_key == "key-b"             # aux pins the NEW credential
        finally:
            client.close()
    finally:
        aux.clear_runtime_main()


def test_D45_index_target_form_resolves(qdir, quota_aware, monkeypatch):
    """D45: a 1-based index names an account, so the config stays usable (no fill_first)."""
    import agent.credential_pool as cp
    monkeypatch.setattr(cp, "get_pool_policy_block", lambda provider: {"metered_overflow": ["1"]})
    cp._POLICY_WARNED.clear()
    a = _entry("a", label="Account A")
    pool = _pool([a, _entry("b", label="Account B")])
    assert pool.resolve_target("1")[1] is a
    policy, usable = pool._quota_policy()
    assert usable is True and policy.earliest_reset_first is True


def test_D45_bare_var_matches_an_env_source_with_a_custom_label(qdir, quota_aware, monkeypatch):
    """D45: a bare VAR is normalised to ``env:<VAR>`` and matches the entry's source, even
    when the account carries a custom label."""
    import agent.credential_pool as cp
    monkeypatch.setattr(cp, "get_pool_policy_block",
                        lambda provider: {"metered_overflow": ["OPENCODE_GO_API_KEY"]})
    cp._POLICY_WARNED.clear()
    a = _entry("a", label="Account A")
    a.source = "env:OPENCODE_GO_API_KEY"
    pool = _pool([a, _entry("b", label="Account B")])
    assert pool.resolve_target("OPENCODE_GO_API_KEY")[1] is a
    assert pool._quota_policy()[1] is True


def test_D45_entry_id_takes_precedence_over_a_label(qdir, quota_aware, monkeypatch):
    """D45: an exact entry id wins over a colliding label — the config is not ambiguous."""
    import agent.credential_pool as cp
    monkeypatch.setattr(cp, "get_pool_policy_block", lambda provider: {"metered_overflow": ["a"]})
    cp._POLICY_WARNED.clear()
    a = _entry("a", label="Account A")
    b = _entry("b", label="a")                           # b's label collides with a's id
    pool = _pool([a, b])
    assert pool.resolve_target("a")[1] is a              # id, not ambiguous
    assert pool._quota_policy()[1] is True


def test_D45_duplicate_label_is_ambiguous_while_the_index_form_resolves(qdir, quota_aware, monkeypatch):
    """D45: two entries sharing a label make that LABEL ambiguous, but the 1-based index form
    still resolves one of them — the collision must not force the whole config to fill_first."""
    import agent.credential_pool as cp
    cp._POLICY_WARNED.clear()
    a = _entry("a", label="Dup")
    b = _entry("b", label="Dup")
    pool = _pool([a, b])
    assert pool.resolve_target("Dup")[1] is None         # the shared label is ambiguous
    monkeypatch.setattr(cp, "get_pool_policy_block", lambda provider: {"metered_overflow": ["1"]})
    assert pool.resolve_target("1")[1] is a              # the index form resolves it
    assert pool._quota_policy()[1] is True               # ...so the config stays usable


def test_D45_valid_index_config_keeps_covered_first(qdir, quota_aware, monkeypatch):
    """A valid D45 config must NOT degrade to fill_first: covered-first still serves B, not
    the priority-0 overflow A that fill_first would hand out."""
    import agent.credential_pool as cp
    monkeypatch.setattr(cp, "get_pool_policy_block", lambda provider: {"metered_overflow": ["1"]})
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)])          # A overflow, priority 0
    _facts("sha256:bbb", [_win(percent=5.0)])            # B covered, priority 1
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa"),
                  _entry("b", priority=1, fingerprint="sha256:bbb")])
    assert pool.select().id == "b"


def test_D46b_warns_again_when_service_returns_to_metered(qdir, quota_aware, caplog):
    """D46(b): the warning tracks the SERVED account and state, so overflow A -> covered B ->
    overflow A warns twice (B reset the state)."""
    _facts("sha256:aaa", [_win(percent=100.0)])          # A overflow
    _facts("sha256:bbb", [_win(percent=100.0)], metered_exhausted_until=NOW.replace(year=2027))
    pool = _pool([_entry("a", fingerprint="sha256:aaa"), _entry("b", fingerprint="sha256:bbb")])
    with caplog.at_level(logging.WARNING):
        assert pool.select().id == "a"                   # A overflow -> warn 1
        _facts("sha256:bbb", [_win(percent=5.0)], metered_exhausted_until=None)
        assert pool.select().id == "b"                   # B covered -> no warn
        _facts("sha256:bbb", [_win(percent=100.0)], metered_exhausted_until=NOW.replace(year=2027))
        assert pool.select().id == "a"                   # A overflow again -> warn 2
    msgs = [r.message for r in caplog.records if "from its metered bucket" in r.message]
    assert len(msgs) == 2


def test_D46b_warns_on_every_transition_into_metered_service(qdir, quota_aware, caplog):
    """D46(b): the warning tracks the SERVED account and state, so a covered account that
    turns overflow warns each time it re-enters metered service (covered -> overflow ->
    covered -> overflow warns twice)."""
    _facts("sha256:aaa", [_win(percent=5.0)])            # A covered
    _facts("sha256:bbb", [_win(percent=100.0)], metered_exhausted_until=NOW.replace(year=2027))
    pool = _pool([_entry("a", fingerprint="sha256:aaa"), _entry("b", fingerprint="sha256:bbb")])
    with caplog.at_level(logging.WARNING):
        assert pool.select().id == "a"                   # A covered -> no warn
        _facts("sha256:aaa", [_win(percent=100.0)])      # A spent -> overflow
        assert pool.select().id == "a"                   # state changed -> warn 1
        _facts("sha256:aaa", [_win(percent=5.0)])        # A covered again
        assert pool.select().id == "a"                   # no warn
        _facts("sha256:aaa", [_win(percent=100.0)])      # A spent again
        assert pool.select().id == "a"                   # re-entered metered -> warn 2
    msgs = [r.message for r in caplog.records if "from its metered bucket" in r.message]
    assert len(msgs) == 2


# ============================================================================
# 10. D45/D49: classification consumes the SAME target resolver as the config
#     check, so an account the user did NOT name is never served from metered.
# ============================================================================


def _classified_states(pool, entries):
    return {eid: state.state for eid, state in pool._quota_states(entries).items()}


def _metered_policy(monkeypatch, block):
    """Point BOTH the config check (``get_pool_policy_block``) and the read-time classification
    (``quota_state._policy_for``) at the same ``metered_overflow`` block."""
    import agent.credential_pool as cp
    monkeypatch.setattr(cp, "get_pool_policy_block", lambda provider: block)
    monkeypatch.setattr(qs, "_policy_for", lambda provider, policy=None: block)
    return block


def test_D45_index_form_classifies_the_resolved_account_metered(qdir, quota_aware, monkeypatch):
    """D45/D49: classification resolves each ``metered_overflow`` target with the SAME precedence
    the config check uses, so the 1-based index marks the resolved account metered and nothing
    else. With both plans spent, the named account is ``overflow`` (its metered cushion) and the
    unnamed one is ``exhausted`` -- never billed."""
    import agent.credential_pool as cp
    _metered_policy(monkeypatch, {"metered_overflow": ["1"]})
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)],
           entries=[{"profile": "default", "entry_id": "a", "label": "Account A"}])
    _facts("sha256:bbb", [_win(percent=100.0)],
           entries=[{"profile": "default", "entry_id": "b", "label": "Account B"}])
    a = _entry("a", fingerprint="sha256:aaa", priority=1)
    b = _entry("b", fingerprint="sha256:bbb", priority=0)
    pool = _pool([a, b])
    assert pool.resolve_target("1")[1] is b              # index 1 = the priority-0 order head
    assert _classified_states(pool, [a, b]) == {"a": "exhausted", "b": "overflow"}
    assert pool.select().id == "b"                       # only the metered cushion is served


def test_D45_entry_id_precedence_classifies_only_the_id_account_metered(qdir, quota_aware, monkeypatch):
    """D45/D49: an exact entry id beats a colliding label in CLASSIFICATION too. Only the id's
    account is metered; the account whose label merely collides is ``exhausted`` and never served
    from its metered bucket."""
    import agent.credential_pool as cp
    _metered_policy(monkeypatch, {"metered_overflow": ["a"]})
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)],
           entries=[{"profile": "default", "entry_id": "a", "label": "Account A"}])
    _facts("sha256:bbb", [_win(percent=100.0)],
           entries=[{"profile": "default", "entry_id": "b", "label": "a"}])
    a = _entry("a", label="Account A", fingerprint="sha256:aaa", priority=1)
    b = _entry("b", label="a", fingerprint="sha256:bbb", priority=0)  # b's label collides with a's id
    pool = _pool([a, b])
    assert pool.resolve_target("a")[1] is a              # the id wins, not the colliding label
    assert _classified_states(pool, [a, b]) == {"a": "overflow", "b": "exhausted"}
    assert pool.select().id == "a"                       # B is never billed


def test_declared_metered_list_excludes_an_unnamed_account_from_billing(qdir, quota_aware, monkeypatch):
    """D49: a declared list is authoritative. A target that names no account meters NOTHING -- the
    provider's ``bills_when_plan_spent`` default must not leak through and bill an unnamed
    account."""
    import agent.credential_pool as cp
    _metered_policy(monkeypatch, {"metered_overflow": ["NO_SUCH_ACCOUNT"]})
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=100.0)])
    a = _entry("a", fingerprint="sha256:aaa", priority=0)
    b = _entry("b", fingerprint="sha256:bbb", priority=1)
    pool = _pool([a, b])
    assert _classified_states(pool, [a, b]) == {"a": "exhausted", "b": "exhausted"}


def test_D46a_warning_states_the_consequence_for_the_declared_shape(
    qdir, quota_aware, monkeypatch, caplog, tmp_path,
):
    """D46(a)/D49: the 'did not take effect' warning states the CONSEQUENCE of the declared shape.
    An unmatched DECLARED list meters nothing; only an ABSENT key lets the provider default
    classify accounts metered."""
    import agent.credential_pool as cp
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    block = _metered_policy(monkeypatch, {"metered_overflow": ["NO_SUCH_ACCOUNT"]})
    cp._POLICY_WARNED.clear()
    _facts("sha256:aaa", [_win(percent=100.0)])
    pool = _pool([_entry("a", fingerprint="sha256:aaa")])
    with caplog.at_level(logging.WARNING):
        pool.select()
        assert len(_d46a_records(caplog)) == 1
        assert "no account is treated as metered" in _d46a_records(caplog)[0].message
        del block["metered_overflow"]                    # the key is now absent
        pool.select()
    recs = _d46a_records(caplog)
    assert len(recs) == 2
    assert "classify metered by provider default" in recs[1].message


def test_quota_choose_endpoint_filter_restricts_the_candidate_set(qdir, quota_aware):
    """D22: ``quota_choose(endpoint=...)`` restricts the candidate set BEFORE the choose/stickiness
    rules run, so it never returns an entry that does not serve the endpoint. The turn-boundary
    path relies on this and needs no second endpoint re-check."""
    from agent.credential_pool import credential_pool_entry_serves_endpoint
    other = "https://elsewhere.example/v1"
    _facts("sha256:aaa", [_win(percent=100.0)])          # A overflow, the session's endpoint
    _facts("sha256:bbb", [_win(percent=5.0)])            # B covered, ANOTHER endpoint
    pool = _pool([_entry("a", fingerprint="sha256:aaa", base_url=SHARED_URL),
                  _entry("b", fingerprint="sha256:bbb", base_url=other)])
    # Unfiltered, the covered sibling on the other endpoint wins the turn-boundary move.
    assert pool.quota_choose(current_id="a", mode="turn_boundary").id == "b"
    # Filtered to the session's endpoint, A stays and B is never a candidate.
    chosen = pool.quota_choose(current_id="a", mode="turn_boundary", endpoint=SHARED_URL)
    assert chosen.id == "a"
    assert credential_pool_entry_serves_endpoint(chosen, SHARED_URL)
    assert pool.quota_choose(current_id="a", mode="turn_boundary", endpoint=other).id == "b"


def test_revert_guard_filters_candidates_by_the_session_endpoint(qdir, quota_aware):
    """D22: the revert guard's quota check filters candidates by the session's endpoint, so a
    covered entry on ANOTHER endpoint cannot cancel a valid revert onto the endpoint's account."""
    from agent.agent_runtime_helpers import _revert_credential_rotation
    other = "https://elsewhere.example/v1"
    _facts("sha256:aaa", [_win(percent=5.0)])            # R: the revert target, covered, same endpoint
    _facts("sha256:bbb", [_win(percent=100.0)])          # C: current binding, overflow, same endpoint
    _facts("sha256:ccc", [_win(percent=5.0)])            # O: covered, OTHER endpoint, sorts first
    pool = _pool([_entry("r", fingerprint="sha256:aaa", priority=1),
                  _entry("c", fingerprint="sha256:bbb", priority=2),
                  _entry("o", fingerprint="sha256:ccc", priority=0, base_url=other)])
    agent = _Agent(pool, "c", revert_id="r")
    _revert_credential_rotation(agent)
    assert agent.swaps == ["r"]                          # the cross-endpoint sibling did not cancel it


# ============================================================================
# Bench escape (D50): a billing bench must never hide an account with quota
# ============================================================================


def _bench(entry, *, code=402, status=STATUS_EXHAUSTED, seconds_ago=60, model=None):
    """Stamp a live cooldown on *entry* as the failed-request path would."""
    entry.last_status = status
    entry.last_status_at = time.time() - seconds_ago
    entry.last_error_code = code
    if model is not None:
        entry.model_cooldowns = {model: time.time() + 3600}
    return entry


def test_a_covered_account_survives_its_own_billing_bench(qdir, quota_aware):
    """Regression: one (possibly mis-attributed) 402 benched a covered account for its whole TTL.

    ``_available_entries`` dropped it from the candidate set, so ``select`` handed out the spent
    sibling and every request 402'd with no way back -- exactly the "stuck on the exhausted
    account" report. The quota view still called this account covered, so it must stay selectable.
    """
    _facts("sha256:aaa", [_win(percent=100.0)])   # A spent -> overflow
    _facts("sha256:bbb", [_win(percent=5.0)])     # B has plan quota left
    a = _entry("a", priority=0, fingerprint="sha256:aaa")
    b = _bench(_entry("b", priority=1, fingerprint="sha256:bbb"))
    pool = _pool([a, b])
    chosen = pool.select()
    assert chosen is not None and chosen.id == "b"


def test_a_fully_benched_pool_still_serves_its_covered_account(qdir, quota_aware):
    """Both accounts benched (the incident's state) -> the covered one is served, never None."""
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_bench(_entry("a", priority=0, fingerprint="sha256:aaa")),
                  _bench(_entry("b", priority=1, fingerprint="sha256:bbb"))])
    chosen = pool.select()
    assert chosen is not None and chosen.id == "b"


def test_a_benched_covered_account_beats_an_available_spent_one(qdir, quota_aware):
    """D50 with both states live: coverage decides, not which sibling happens to be un-benched."""
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa"),
                  _bench(_entry("b", priority=1, fingerprint="sha256:bbb"))])
    assert pool.quota_choose(current_id="a", mode="turn_boundary").id == "b"


def test_the_bench_escape_never_re_admits_a_dead_credential(qdir, quota_aware):
    """Only a *billing* bench is escapable: a revoked credential stays out of the pool."""
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_bench(_entry("b", fingerprint="sha256:bbb"), code=401, status=STATUS_DEAD)])
    assert pool.select() is None


def test_the_bench_escape_respects_a_model_scoped_cooldown(qdir, quota_aware):
    """A 429 on one model is not a billing bench: the account stays out for that model."""
    _facts("sha256:bbb", [_win(percent=5.0)])
    _facts("sha256:aaa", [_win(percent=5.0)])
    b = _bench(_entry("b", priority=1, fingerprint="sha256:bbb"), code=429, model="glm-5",
               seconds_ago=1)
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa"), b])
    assert pool.select(model="glm-5").id != "b"


def test_the_bench_escape_leaves_an_unbenched_pool_untouched(qdir, quota_aware):
    """No bench, no escape: ordinary selection still prefers the lower-priority covered account."""
    _facts("sha256:aaa", [_win(percent=5.0)])
    _facts("sha256:bbb", [_win(percent=90.0)])
    pool = _pool([_entry("a", priority=0, fingerprint="sha256:aaa"),
                  _entry("b", priority=1, fingerprint="sha256:bbb")])
    assert pool.select().id == "a"


def test_the_bench_escape_is_visible_to_has_available(qdir, quota_aware):
    """``has_available``/``next_available_at`` gate ``select`` for callers like the restore path.

    A False there sent them to the provider's static key even though the pool could serve the
    covered account, so the escape must show up in both or the stall only half-disappears.
    """
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_bench(_entry("a", priority=0, fingerprint="sha256:aaa")),
                  _bench(_entry("b", priority=1, fingerprint="sha256:bbb"))])
    assert pool.has_available() is True
    assert pool.next_available_at() is None      # nothing to wait for: B can serve now
    # Still honest when the only benched account has no quota left: nothing available.
    _facts("sha256:bbb", [_win(percent=100.0)])
    spent = _pool([_bench(_entry("b", fingerprint="sha256:bbb"))])
    assert spent.has_available() is False
    assert spent.select() is None
    assert spent.next_available_at() is not None  # report the wait instead of pretending


def test_a_live_covered_account_outranks_a_benched_covered_sibling(qdir, quota_aware):
    """A just-failed credential must not shadow a healthy sibling on priority (the escape's own
    stall: the same account would be re-picked on every request, 402ing forever)."""
    _facts("sha256:aaa", [_win(percent=5.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_bench(_entry("b", priority=0, fingerprint="sha256:bbb")),
                  _entry("a", priority=1, fingerprint="sha256:aaa")])
    assert pool.select().id == "a"
    # ... and rotation therefore still has somewhere to go (#97315's guard must not fire).
    nxt = pool.mark_exhausted_and_rotate(status_code=402, api_key_hint="key-b", credential_id="b")
    assert nxt is not None and nxt.id == "a"


def test_a_benched_entry_without_quota_is_never_re_admitted(qdir, quota_aware):
    """Only a *covered* verdict earns the escape: UNKNOWN (no facts) and OVERFLOW stay benched."""
    _facts("sha256:aaa", [_win(percent=100.0)])   # stored facts say spent
    unknown = _pool([_bench(_entry("u", fingerprint="sha256:unknown"))])
    assert unknown.select() is None               # no facts at all -> UNKNOWN
    overflow = _pool([_bench(_entry("a", fingerprint="sha256:aaa"))])
    assert overflow.select() is None


def test_the_bench_survives_the_escape(qdir, quota_aware):
    """The account is offered to the ordering, but its failure record is kept: the bench still
    guards the paths that do not consult the quota view, and rotation keeps its no-recovery guard."""
    _facts("sha256:bbb", [_win(percent=5.0)])
    b = _bench(_entry("b", fingerprint="sha256:bbb"))
    stamped = b.last_status_at
    pool = _pool([b])
    assert pool.select().id == "b"
    kept = pool.entries()[0]
    assert kept.last_status == STATUS_EXHAUSTED and kept.last_error_code == 402
    assert kept.last_status_at == stamped


def test_acquire_lease_readmits_a_benched_covered_account(qdir, quota_aware):
    """Leases draw their candidates from the same set as ``select`` (D44)."""
    _facts("sha256:aaa", [_win(percent=100.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_bench(_entry("a", priority=0, fingerprint="sha256:aaa")),
                  _bench(_entry("b", priority=1, fingerprint="sha256:bbb"))])
    assert pool.acquire_lease() == "b"


def test_reclaim_readmits_a_benched_covered_account(qdir, quota_aware):
    """The revert path asks "may I go back to THIS credential?" through ``reclaim``.

    A targeted query, so a benched-but-covered target answers yes even while another covered
    account is free — the revert guard applies its own D22 covered check before swapping.
    """
    _facts("sha256:aaa", [_win(percent=5.0)])
    _facts("sha256:bbb", [_win(percent=5.0)])
    pool = _pool([_bench(_entry("b", fingerprint="sha256:bbb")),
                  _entry("a", fingerprint="sha256:aaa")])
    assert pool.reclaim("b") is not None
