"""A credential the provider refuses claims no capacity row (spec D51).

The capacity row exists to show quota, so it needs two answers from the same signal: a plan that is
SPENT keeps its row (the windows already say so), and a credential the provider refuses as
unauthenticated/unentitled loses its row - its numbers can no longer be refreshed and its account
is gone. Everything in between (a rate-limited read, an edge/WAF block, a missing OAuth scope, a
network error) is a READ problem: the row stays, marked as it always was.

These are the behaviour contracts for the classification (``failure_kind`` / ``error_kind`` /
``hides_row``), for the stored record carrying it, and for the wire filter over legacy groups and
per-account views. Selection is deliberately untouched (D7 fail-open): only the display drops.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from agent import quota_state as qs
from agent import quota_wire

NOW = datetime.now(UTC)
PROVIDER = "opencode-go"


def _window(period="7d", percent=0.0, days=3, status=None):
    return {"period": period, "used_percent": percent, "status": status,
            "reset_at": (NOW + timedelta(days=days)).isoformat(), "window_seconds": 7 * 86400}


@pytest.fixture
def qdir(tmp_path, monkeypatch):
    """A temp, network-free quota dir (no fetch, no credential store)."""
    root = tmp_path / "quota"
    monkeypatch.setattr(qs, "quota_dir", lambda: root)
    monkeypatch.setattr(qs, "default_fetcher", lambda provider: pytest.fail("real fetch"))
    monkeypatch.setattr(qs, "default_connected", lambda provider: True)
    monkeypatch.setattr(qs, "default_multi_account", lambda provider: False)
    monkeypatch.setattr(qs, "default_live_fingerprints", lambda provider: None)
    monkeypatch.setattr(qs, "credential_files", list)
    qs._last_touch.clear()
    qs._connected_cache.clear()
    return root


# ============================================================================
# Classification of one failed read
# ============================================================================

@pytest.mark.parametrize(("status", "message", "kind"), [
    (401, "OAuth access token has expired. Re-authenticate to continue.", "auth"),
    (402, "payment required", "auth"),
    (429, "Too many requests", "rate_limit"),
    (403, '{"error":"You do not have an active subscription"}', "unentitled"),
    (403, '{"cloudflare_error":true,"error_name":"browser_signature_banned"}', "blocked"),
    (403, "Error 1010: Access denied", "blocked"),
    (403, '{"error":{"type":"permission_error","message":"OAuth token does not meet scope '
          'requirement user:profile"}}', "scope"),
    (0, "No available openai-codex credential in credential pool", "auth"),
    (500, "Internal Server Error", "transient"),
    (0, "Connection reset by peer", "transient"),
    (None, "", "transient"),
])
def test_failure_kind_reads_the_body_not_only_the_status(status, message, kind):
    assert qs.failure_kind(status, message) == kind


def test_fetch_error_keeps_the_body_it_classified():
    """The body exists only at fetch time; the stored record has to carry the verdict."""
    request = httpx.Request("GET", "https://api.example.test/usage")
    response = httpx.Response(403, request=request,
                              text='{"error":"Your subscription is inactive"}')
    exc = httpx.HTTPStatusError("403 Forbidden", request=request, response=response)

    err = qs.QuotaFetchError.from_exception("anthropic", exc)

    assert (err.status, err.kind) == (403, "unentitled")
    assert "subscription is inactive" in err.message


@pytest.mark.parametrize(("error", "kind", "hidden"), [
    ({"status": 403, "kind": "auth"}, "auth", True),
    ({"status": 403, "kind": "unentitled"}, "unentitled", True),
    ({"status": 403, "kind": "blocked"}, "blocked", False),
    ({"status": 403, "kind": "scope"}, "scope", False),
    ({"status": 429, "kind": "rate_limit"}, "rate_limit", False),
    # A record written before the classification existed: the two unambiguous kinds are derived,
    # a bare 403 is NOT guessed at (it may have been an edge block).
    ({"status": 401, "message": "Unauthorized"}, "auth", True),
    ({"status": 0, "message": "No available openai-codex credential in credential pool"}, "auth", True),
    ({"status": 403, "message": "Client error '403 Forbidden' for url 'https://x/usage'"}, None, False),
    ({"status": 0, "message": "multi_account"}, None, False),
    (None, None, False),
])
def test_stored_error_record_hides_a_row_only_when_it_says_so(error, kind, hidden):
    assert qs.error_kind(error) == kind
    assert qs.hides_row(error) is hidden


# ============================================================================
# Legacy (non-quota_aware) provider groups
# ============================================================================

def _snapshot(provider, *, error, windows=None):
    qs._write_json_atomic(qs._path("snapshot", provider, ".json"), {
        "provider": provider,
        "fetched_at": NOW.isoformat(),
        "attempted_at": NOW.isoformat(),
        "windows": windows if windows is not None else [_window(percent=77.0)],
        "error": error,
        "backoff_until": None,
    })


def test_unentitled_legacy_provider_claims_no_row(qdir, monkeypatch):
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: False)
    _snapshot("anthropic", error={"status": 401, "kind": "auth",
                                  "message": "OAuth access token has expired"})

    assert quota_wire.all_groups() == []


def test_blocked_read_keeps_the_row_it_cannot_refresh(qdir, monkeypatch):
    """A 403 from the edge is not an entitlement verdict: the row and its numbers stay."""
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: False)
    _snapshot("opencode-go", error={"status": 403, "kind": "blocked",
                                    "message": "cloudflare browser_signature_banned"})

    groups = quota_wire.all_groups()

    assert [g["provider"] for g in groups] == ["opencode-go"]
    assert [w["used_percent"] for w in groups[0]["windows"]] == [77.0]


def test_spent_plan_keeps_its_row(qdir, monkeypatch):
    """The other half of the rule: quota that is USED UP is exactly what the row is for."""
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: False)
    _snapshot("anthropic", error={"status": 429, "kind": "rate_limit"},
              windows=[_window(percent=100.0, status="rate-limited")])

    groups = quota_wire.all_groups()

    assert [g["provider"] for g in groups] == ["anthropic"]
    assert [w["used_percent"] for w in groups[0]["windows"]] == [100.0]


# ============================================================================
# Per-account views (quota_aware pools)
# ============================================================================

def _facts(fp, *, windows=None, error=None):
    qs.store_account_facts(
        PROVIDER, fp, windows if windows is not None else [_window(percent=11.0)],
        entries=[{"profile": "default", "entry_id": fp[-3:], "label": fp[-3:]}],
        fetched_at=NOW.isoformat(), attempted_at=NOW.isoformat(), error=error, now=NOW,
    )


def test_unentitled_account_claims_no_sub_row(qdir, monkeypatch):
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)
    _facts("sha256:aaa", error={"status": 401, "kind": "auth", "message": "token has expired"})
    _facts("sha256:bbb", windows=[_window(percent=100.0, status="rate-limited")],
           error={"status": 429, "kind": "rate_limit"})

    groups = [g for g in quota_wire.view_groups() if g["provider"] == PROVIDER]

    assert len(groups) == 1
    assert groups[0] is not None
    assert [a["fingerprint"] for a in groups[0]["accounts"]] == ["sha256:bbb"]
    assert groups[0]["windows"][0]["status"] == "rate-limited"


def test_a_pool_in_hiding_state_claims_no_capacity_row(qdir, monkeypatch):
    """The refused credential must not come back through the legacy status line.

    ``status_groups`` builds a ``quota_aware`` provider's legacy line from the SERVING account's
    windows and attaches no error, so the row survives a filter that only reads ``error``. A
    cancelled subscription then renders as a clean row of rolled-forward zeros, which reads as
    "plenty left" - the exact opposite of the truth.
    """
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)
    _facts("sha256:aaa", error={"status": 403, "kind": "unentitled",
                                "message": "OpenCode Go subscription required."})

    assert [g["provider"] for g in qs.status_groups()] == [PROVIDER]  # the unfiltered line exists
    assert quota_wire.all_groups() == []


def test_a_spent_pool_keeps_its_capacity_row(qdir, monkeypatch):
    """The same path must still show quota that is USED UP: hiding is for refused credentials."""
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)
    _facts("sha256:bbb", windows=[_window(percent=100.0, status="rate-limited")],
           error={"status": 429, "kind": "rate_limit"})

    groups = quota_wire.all_groups()

    assert [g["provider"] for g in groups] == [PROVIDER]
    assert [w["used_percent"] for w in groups[0]["windows"]] == [100.0]


def test_a_paused_lane_claims_no_row(qdir, monkeypatch):
    """Pausing a lane drops its credential but keeps its facts; the row must go with the credential.

    ``read_view`` keeps reading those facts on purpose (selection stays fail-open, D7), so the wire
    is the only place that can tell "this account is not live any more" apart from "unknown".
    """
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)
    monkeypatch.setattr(qs, "default_connected", lambda provider: False)
    _facts("sha256:aaa", windows=[_window(percent=11.0)])

    assert quota_wire.all_groups() == []
    # The selection view is untouched: the account is still there for the pool.
    view = qs.read_view("default", PROVIDER, now=NOW)
    assert view and view["accounts"]


def test_selection_still_sees_the_unentitled_account(qdir, monkeypatch):
    """D7 stays: the display drops the row, the pool keeps the account (fail-open, no eviction)."""
    monkeypatch.setattr(qs, "_is_quota_aware", lambda provider: provider == PROVIDER)
    _facts("sha256:aaa", error={"status": 401, "kind": "auth"})

    view = qs.read_view("default", PROVIDER, now=NOW)

    assert view is not None
    assert [a["fingerprint"] for a in view["accounts"]] == ["sha256:aaa"]
    assert view["accounts"][0]["error_kind"] == "auth"


def _view(accounts, serving):
    return {"provider": PROVIDER, "strategy": "quota_aware", "serving": serving,
            "serving_since": NOW.isoformat(), "accounts": accounts,
            "states": {"covered": 0, "overflow": 0, "exhausted": 0}, "windows": []}


def _account(fp, *, windows=None, error_kind=None):
    return {"fingerprint": fp, "label": fp[-3:], "state": "covered", "metered": False,
            "read_health": "ok" if error_kind is None else "error",
            "windows": windows if windows is not None else [_window(percent=42.0)],
            "fetched_at": NOW.isoformat(), "age_s": 60, "error_kind": error_kind,
            "last_error": None, "recovery_at": None}


def test_a_pool_of_only_unentitled_accounts_claims_no_row():
    view = _view([_account("sha256:aaa", error_kind="auth")], serving="sha256:aaa")

    assert quota_wire.group_from_view(PROVIDER, view) is None


def test_the_line_falls_back_when_the_serving_account_is_unentitled():
    """The provider line never carries a hidden account's numbers."""
    view = _view([_account("sha256:aaa", error_kind="auth"),
                  _account("sha256:bbb", windows=[_window(percent=7.0)])],
                 serving="sha256:aaa")

    group = quota_wire.group_from_view(PROVIDER, view)

    assert group is not None
    assert [a["fingerprint"] for a in group["accounts"]] == ["sha256:bbb"]
    assert [w["used_percent"] for w in group["windows"]] == [7.0]
    assert group["age_s"] == 60
