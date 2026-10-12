"""Shared, cross-process subscription-quota state.

One subscription belongs to the user, not to a profile, so this state is HOME-anchored
(``~/.hermes/quota``, like the profiles root) and shared by every Hermes process and profile:

- ``snapshot/<provider>.json`` — last fetched windows (kept on fetch failure) + error/backoff.
- ``dirty/<provider>`` — mtime = last billed API call to that provider, from any process.
- ``history.jsonl`` — one line per successful fetch (burn-rate input for routers).
- ``lock/<provider>.lock`` — flock held while fetching, so N processes fetch once.

Refresh rule: fetch only when the provider was called since the last fetch (dirty) or has never
been fetched (bootstrap), never inside a 60 s floor or a backoff window. Uncalled providers are
never re-fetched; readers roll a window past its reset forward instead (resets are deterministic).
This is what keeps the rate-limited usage endpoints (Anthropic's answers 429 with Retry-After of
minutes) from being stormed by a dozen TUI gateways refreshing after every turn.
"""

from __future__ import annotations

import contextlib
import fcntl
import itertools
import json
import logging
import os
import random
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from agent import quota_aware_state as qstate

logger = logging.getLogger(__name__)

SUPPORTED = ("anthropic", "openai-codex", "opencode-go")
FLOOR_S = 60
DIRTY_THROTTLE_S = 30
BACKOFF_429_DEFAULT_S = 300
BACKOFF_CAP_S = 3600
BACKOFF_ERR_START_S = 60
BACKOFF_ERR_CAP_S = 1800
LOCK_WAIT_S = 20.0
HISTORY_MAX_BYTES = 4 * 1024 * 1024
# A per-profile view's serving history is a bounded burn-rate series: ``load_pool(
# quota_bootstrap=True)`` recomposes and would otherwise append a sample on every billed aux
# call, growing the view file without limit on the request path. Cap the sample count so the
# file stays bounded however many calls pass (D35).
VIEW_HISTORY_MAX_SAMPLES = 64

# Display label -> canonical period; window length by period. Unmapped labels are preserved
# as their own period (D25) rather than dropping the window.
_PERIOD = {
    "session": "5h", "current session": "5h", "5h": "5h", "rolling window": "5h",
    "weekly": "7d", "current week": "7d", "7d": "7d",
    "monthly": "monthly",
    "opus week": "opus 7d", "sonnet week": "sonnet 7d",
    "api key quota": "api key quota",
}
_SECONDS = {"5h": 5 * 3600, "7d": 7 * 86400, "opus 7d": 7 * 86400, "sonnet 7d": 7 * 86400}

_last_touch: dict[str, float] = {}
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def quota_dir() -> Path:
    """HOME-anchored so every profile shares one subscription's state. ``HERMES_QUOTA_DIR``
    overrides; under the test sandbox (``HERMES_TEST_ISOLATION``) it follows HERMES_HOME so a
    test never touches the user's real quota state."""
    override = os.environ.get("HERMES_QUOTA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if os.environ.get("HERMES_TEST_ISOLATION") and os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]) / "quota"
    return Path.home() / ".hermes" / "quota"


def _dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class QuotaFetchError(Exception):
    provider: str
    status: int                      # HTTP status, 0 = network/other
    retry_after_s: Optional[int]
    message: str

    def __str__(self) -> str:
        return f"{self.provider} quota fetch failed ({self.status}): {self.message}"

    @classmethod
    def from_exception(cls, provider: str, exc: BaseException) -> "QuotaFetchError":
        if isinstance(exc, QuotaFetchError):
            return exc
        response = getattr(exc, "response", None)
        status = int(getattr(response, "status_code", 0) or 0)
        retry_after = None
        if response is not None:
            raw = (getattr(response, "headers", None) or {}).get("Retry-After")
            try:
                retry_after = int(float(raw)) if raw is not None else None
            except ValueError:
                retry_after = None
        return cls(provider, status, retry_after, str(exc)[:300])


# ---------------------------------------------------------------- files

def _path(kind: str, provider: str, suffix: str = "") -> Path:
    return quota_dir() / kind / f"{provider}{suffix}"


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path)


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


def _append_history(row: dict) -> None:
    path = quota_dir() / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > HISTORY_MAX_BYTES:
            os.replace(path, path.with_suffix(".jsonl.1"))
    except FileNotFoundError:
        pass
    line = json.dumps(row, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def _read_raw(provider: str) -> Optional[dict]:
    try:
        return json.loads(_path("snapshot", provider, ".json").read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- public API

def quota_provider(provider: Optional[str], base_url: Optional[str] = None) -> str:
    """The subscription a call bills to: an opencode.ai/zen/go endpoint IS the Go subscription
    whatever the provider entry is named (mirrors ``account_usage.fetch_account_usage``)."""
    url = str(base_url or "").lower()
    if "opencode.ai" in url and "/zen/go" in url:
        return "opencode-go"
    return str(provider or "").strip().lower()


def mark_called(provider: Optional[str], now: Optional[datetime] = None, *,
                base_url: Optional[str] = None, fingerprint: Optional[str] = None,
                api_key: Optional[str] = None, entry_id: Optional[str] = None) -> None:
    """Record that a billed API call just went to ``provider`` (never raises).

    The provider dirty mark is throttled; the per-account refresh enqueue is not, so an account
    served moments after another is still attributed (D42). Callers pass whatever they cheaply
    know (``api_key`` / ``entry_id`` / ``base_url``); when none resolves to a fingerprint the
    call is still marked dirty.
    """
    name = quota_provider(provider, base_url)
    if name not in SUPPORTED:
        return
    ts = (now or _utc_now()).timestamp()
    last = _last_touch.get(name, -1e18)
    if ts - last < DIRTY_THROTTLE_S:
        # Within the throttle: the existing mark is still effective while it is newer than the
        # last fetch; a mark a fetch has since spent is re-touched so the call is not lost.
        fetched = _dt((_read_raw(name) or {}).get("fetched_at"))
        touch = fetched is not None and last <= fetched.timestamp()
    else:
        touch = True
    if touch:
        _last_touch[name] = ts
        try:
            path = _path("dirty", name)
            _touch(path)
            if now is not None:
                os.utime(path, (ts, ts))
        except OSError:
            logger.debug("quota dirty mark failed for %s", name, exc_info=True)
    # Only a quota_aware provider keeps per-account facts: resolving and enqueuing for any other
    # provider would create facts files, add usage-endpoint traffic and run a pool load on the
    # request path for a provider that is byte-identical to before this change.
    if not _is_quota_aware(name):
        return
    resolved = fingerprint
    if resolved is None:
        resolved = served_fingerprint(name, api_key=api_key, entry_id=entry_id, base_url=base_url)
    if resolved:
        enqueue_refresh(name, resolved)
        note_serving(name, resolved, now=now)


def _dirty_at(provider: str) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(_path("dirty", provider).stat().st_mtime, timezone.utc)
    except OSError:
        return None


def _windows_from_snapshot(snapshot) -> list[dict]:
    out = []
    raw_rate = ((getattr(snapshot, "raw", None) or {}).get("rate_limit") or {})
    codex_seconds = {}
    for key in ("primary_window", "secondary_window"):
        w = raw_rate.get(key) or {}
        if w.get("limit_window_seconds"):
            codex_seconds[int(w["limit_window_seconds"])] = True
    for w in getattr(snapshot, "windows", ()) or ():
        if w.used_percent is None:
            continue
        label = str(w.label).strip().lower()
        # Preserve unmapped labels (e.g. OpenRouter's "API key quota") as their own period
        # instead of dropping the window (D25).
        period = _PERIOD.get(label, label)
        reset = w.reset_at
        seconds = _SECONDS.get(period)
        if period == "monthly" and reset is not None:
            seconds = int((reset - _month_before(reset)).total_seconds())
        status = getattr(w, "status", None)
        out.append({"period": period, "used_percent": float(w.used_percent),
                    "status": (str(status).strip() or None) if status is not None else None,
                    "reset_at": reset.isoformat() if reset else None, "window_seconds": seconds})
    return out


def _month_before(dt: datetime) -> datetime:
    year, month = (dt.year, dt.month - 1) if dt.month > 1 else (dt.year - 1, 12)
    day = min(dt.day, 28)
    return dt.replace(year=year, month=month, day=day)


def _in_backoff(record: Optional[dict], now: datetime) -> bool:
    """The one backoff gate: a stored ``backoff_until`` still in the future means the endpoint
    asked us to wait. Shared by the legacy snapshot path and the per-account facts path so the
    two can never drift."""
    backoff = _dt((record or {}).get("backoff_until"))
    return backoff is not None and now < backoff


def _backoff_wait(record: Optional[dict], err: "QuotaFetchError") -> int:
    """The one backoff computation for a failed fetch: a 429 honours Retry-After (else the
    default); any other error doubles the previous wait up to the cap."""
    if err.status == 429:
        return min(BACKOFF_CAP_S, err.retry_after_s or BACKOFF_429_DEFAULT_S)
    prev_wait = int(((record or {}).get("error") or {}).get("backoff_s") or 0)
    return min(BACKOFF_ERR_CAP_S, prev_wait * 2 if prev_wait else BACKOFF_ERR_START_S)


def _decide(provider: str, now: datetime, connected: Callable[[], bool]) -> str:
    """``connected`` is only evaluated when a fetch would happen (it may resolve credentials)."""
    if provider not in SUPPORTED:
        return "unsupported"
    snap = _read_raw(provider)
    if snap is None or snap.get("multi_account"):
        return "fetch" if connected() else "not_connected"
    if _in_backoff(snap, now):
        return "backoff"
    last = _dt(snap.get("attempted_at") or snap.get("fetched_at"))
    dirty = _dirty_at(provider)
    needs = snap.get("error") is not None or (dirty is not None and (last is None or dirty > last))
    if not needs:
        return "fresh"
    if last is not None and (now - last).total_seconds() < FLOOR_S:
        return "floor"
    return "fetch" if connected() else "not_connected"


def _mark_stale_dirty(provider: str, now: datetime, stale_s: int) -> None:
    snap = _read_raw(provider)
    fetched = _dt((snap or {}).get("fetched_at"))
    if snap is not None and (fetched is None or (now - fetched).total_seconds() > stale_s):
        path = _path("dirty", provider)
        try:
            _touch(path)
            os.utime(path, (now.timestamp(), now.timestamp()))
        except OSError:
            pass


def _thread_lock(provider: str) -> threading.Lock:
    with _thread_locks_guard:
        return _thread_locks.setdefault(provider, threading.Lock())


def default_fetcher(provider: str):
    """Fetch through the provider's account-usage fetcher, raising QuotaFetchError on failure."""
    from agent.account_usage import fetch_account_usage_checked
    return fetch_account_usage_checked(provider)


def default_fetcher_for(provider: str, *, api_key: Optional[str] = None,
                        base_url: Optional[str] = None):
    """Fetch ONE account's usage through the provider hook with that account's own credential."""
    from agent.account_usage import fetch_account_usage_checked
    return fetch_account_usage_checked(provider, base_url=base_url, api_key=api_key)


def _pool_entry(provider: str, fingerprint: str):
    """The pool entry carrying *fingerprint*, read WITHOUT the quota bootstrap: resolving a
    credential must never re-queue fingerprints or touch the profile view."""
    try:
        from agent.credential_pool import load_pool
        for entry in load_pool(provider, quota_bootstrap=False).entries():
            if entry.extra.get("secret_fingerprint") == fingerprint:
                return entry
    except Exception:  # noqa: BLE001
        logger.debug("per-entry pool lookup failed for %s/%s", provider, fingerprint, exc_info=True)
    return None


def _fetcher_for_entry(provider: str, entry) -> Callable:
    """A fetcher using the entry's OWN credential. An unresolved fingerprint FAILS CLOSED:
    falling back to the provider default would fetch - and attribute - another account."""
    if entry is None:
        raise QuotaFetchError(provider, 0, None, "no pool entry for this credential fingerprint")
    api_key = entry.runtime_api_key
    base_url = entry.runtime_base_url
    return lambda: default_fetcher_for(provider, api_key=api_key, base_url=base_url)


def _account_fetcher(provider: str, fingerprint: str) -> Callable:
    return _fetcher_for_entry(provider, _pool_entry(provider, fingerprint))


def default_connected(provider: str) -> bool:
    from agent.account_usage import provider_quota_connected
    return provider_quota_connected(provider)


def default_multi_account(provider: str) -> bool:
    from agent.account_usage import provider_quota_multi_account
    return provider_quota_multi_account(provider)


def default_live_fingerprints(provider: str) -> Optional[tuple[set, set]]:
    from agent.account_usage import provider_quota_live_fingerprints
    return provider_quota_live_fingerprints(provider)


MULTI_ACCOUNT_ERROR = {"status": 0, "message": "multi_account"}


def _store_multi_account(provider: str, now: datetime) -> None:
    """A pool of several accounts has no single quota; store it as unknown (no windows) so
    routers reading the snapshot never mistake one account's usage for the subscription's.

    A provider on the ``quota_aware`` strategy is exempt: its per-account facts + per-profile
    view own the state, and collapsing it to an empty window list is exactly the emptiness this
    change removes (D41)."""
    if _is_quota_aware(provider):
        return
    prev = _read_raw(provider) or {}
    if prev.get("multi_account") and not prev.get("windows"):
        return
    payload = {
        "provider": provider, "fetched_at": None, "attempted_at": now.isoformat(),
        "windows": [], "error": None, "backoff_until": None, "multi_account": True,
    }
    # Name the pool's size from the live credentials so the status bar can say `·2 accounts`
    # instead of a bare unknown. Omit the field entirely when the store cannot name the accounts.
    live = default_live_fingerprints(provider)
    if live is not None:
        payload["multi_account_count"] = len(live[0])
    _write_json_atomic(_path("snapshot", provider, ".json"), payload)


def refresh(provider: str, *, fetcher: Optional[Callable] = None,
            connected: Optional[Callable[[str], bool]] = None, now: Optional[datetime] = None,
            stale_s: Optional[int] = None, multi_account: Optional[Callable[[str], bool]] = None) -> str:
    """Apply the refresh rule for ``provider``. Returns what happened:
    fetched | fresh | floor | backoff | error | busy | not_connected | unsupported | multi_account.

    ``stale_s`` (router freshness, U-1): also fetch when the last successful fetch is older than
    this, even if the provider was not called — the caller bounds how often it asks."""
    name = str(provider or "").strip().lower()
    now = now or _utc_now()
    if name not in SUPPORTED:
        return "unsupported"
    if _is_quota_aware(name):
        # D41: a quota_aware provider owns per-account facts/views. A single-account legacy
        # snapshot would present one account's usage as the whole subscription's, so it is
        # never written; the per-account path owns this provider's state.
        return "quota_aware"
    check = connected or default_connected
    memo: dict = {}

    def is_connected() -> bool:
        if "v" not in memo:
            memo["v"] = bool(check(name))
        return memo["v"]

    if (multi_account or default_multi_account)(name):
        _store_multi_account(name, now)
        return "multi_account"
    if stale_s is not None:
        _mark_stale_dirty(name, now, stale_s)
    first = _decide(name, now, is_connected)
    if first != "fetch":
        return first
    lock_path = _path("lock", name, ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    tlock = _thread_lock(name)
    if not tlock.acquire(timeout=LOCK_WAIT_S):
        return "busy"
    try:
        with open(lock_path, "w", encoding="utf-8") as fh:
            deadline = time.monotonic() + LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        return "busy"
                    time.sleep(0.1)
            try:
                # Another process may have fetched while we waited.
                decision = _decide(name, now, is_connected)
                if decision != "fetch":
                    return decision
                return _fetch_and_store(name, fetcher or default_fetcher, now)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        tlock.release()


def _fetch_and_store(provider: str, fetcher: Callable, now: datetime) -> str:
    prev = _read_raw(provider) or {}
    try:
        snapshot = fetcher(provider)
    except Exception as exc:  # noqa: BLE001 — classified below, never swallowed silently
        err = QuotaFetchError.from_exception(provider, exc)
        wait = _backoff_wait(prev, err)
        logger.debug("quota fetch %s failed: status=%s retry_after=%s backoff=%ss",
                     provider, err.status, err.retry_after_s, wait)
        _write_json_atomic(_path("snapshot", provider, ".json"), {
            "provider": provider, "fetched_at": prev.get("fetched_at"), "attempted_at": now.isoformat(),
            "windows": prev.get("windows") or [],
            "error": {"status": err.status, "message": err.message, "backoff_s": wait},
            "backoff_until": (now + timedelta(seconds=wait)).isoformat(),
        })
        return "error"
    # None = provider answered "nothing to report" (no usage endpoint for this credential): store
    # an empty snapshot so bootstrap does not re-fetch it on every turn.
    windows = _windows_from_snapshot(snapshot) if snapshot is not None else []
    _write_json_atomic(_path("snapshot", provider, ".json"), {
        "provider": provider, "fetched_at": now.isoformat(), "attempted_at": now.isoformat(),
        "windows": windows, "error": None, "backoff_until": None,
    })
    if windows:
        _append_history({"t": now.isoformat(), "provider": provider, "windows": windows})
    return "fetched"


def _rolled(window: dict, now: datetime) -> dict:
    reset = _dt(window.get("reset_at"))
    seconds = window.get("window_seconds")
    if reset is None or not seconds or reset > now:
        return window
    while reset <= now:
        reset += timedelta(seconds=int(seconds))
    # The provider's spent marker belonged to the window that just ended; carrying it into the
    # fresh period would keep the account spent for another full window (D9).
    return {**window, "used_percent": 0.0, "reset_at": reset.isoformat(),
            "status": None, "rolled": True}


def read_snapshot(provider: str, now: Optional[datetime] = None) -> Optional[dict]:
    """Snapshot as stored, with windows past their reset rolled forward to 0%.

    A quota_aware provider owns per-account facts/views, so it never answers with the legacy
    single-account snapshot an earlier build left behind: that snapshot presents one account's
    usage as the whole subscription's, and nothing refreshes it any more (D41). The stored bytes
    stay readable through :func:`_read_raw` for callers that need the raw store."""
    if _is_quota_aware(str(provider or "").strip().lower()):
        return None
    snap = _read_raw(provider)
    if snap is None:
        return None
    now = now or _utc_now()
    return {**snap, "windows": [_rolled(w, now) for w in snap.get("windows") or []]}


def read_all(now: Optional[datetime] = None) -> list[dict]:
    """Every stored snapshot with windows, in ``SUPPORTED`` order. A quota_aware provider is never
    listed: its per-account view is the only source for it (D41)."""
    out = []
    for provider in SUPPORTED:
        snap = read_snapshot(provider, now)
        if snap and snap.get("windows"):
            out.append(snap)
    return out


LABEL_ORDER = ("anthropic", "openai-codex", "opencode-go")
STALE_DISPLAY_S = 1800


def _route_hint(groups: list[dict], now: datetime) -> Optional[str]:
    """Suggest starting on sol while Anthropic runs well ahead of pace and Codex behind."""
    def pace(provider: str) -> Optional[float]:
        for g in groups:
            if g["provider"] != provider:
                continue
            for w in g["windows"]:
                if w["period"] == "7d" and w.get("reset_at"):
                    reset = _dt(w["reset_at"])
                    left = max(0.0, (reset - now).total_seconds()) if reset else None
                    if left is None:
                        return None
                    elapsed = max(1.0, 7 * 86400 - left)
                    return float(w["used_percent"]) * (7 * 86400) / elapsed
        return None
    a, c = pace("anthropic"), pace("openai-codex")
    if a is not None and c is not None and a > 110 and c < 90:
        return "→ sol"
    return None


_connected_cache: dict[str, tuple[float, tuple, bool, bool, Optional[tuple[set, set]]]] = {}
CONNECTED_CACHE_S = 300


def credential_files() -> list[Path]:
    """Local files a subscription credential can come from; their mtimes key the connected cache
    so a login/logout is seen on the next read instead of up to ``CONNECTED_CACHE_S`` later."""
    paths: list[Path] = []
    for resolve in (_hermes_auth_path, _hermes_env_path, _claude_credentials_path, _codex_auth_path):
        try:
            paths.append(resolve())
        except Exception:  # noqa: BLE001
            logger.debug("credential path resolution failed", exc_info=True)
    return paths


def _hermes_auth_path() -> Path:
    from hermes_cli.auth import _auth_file_path
    return _auth_file_path()


def _hermes_env_path() -> Path:
    from hermes_cli.config import get_env_path
    return get_env_path()


def _claude_credentials_path() -> Path:
    from agent.anthropic_credentials import claude_code_credentials_path
    return claude_code_credentials_path()


def _codex_auth_path() -> Path:
    codex_home = os.getenv("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    return Path(codex_home).expanduser() / "auth.json"


def _credential_signature() -> tuple:
    out = []
    for path in credential_files():
        try:
            out.append((str(path), path.stat().st_mtime_ns))
        except OSError:
            out.append((str(path), None))
    return tuple(out)


def _credential_state(provider: str, now: datetime) -> tuple[bool, bool, Optional[tuple[set, set]]]:
    """``(connected, multi_account, live_fingerprints)`` from local credentials (no network), cached
    because the status bar reads every turn; a credential file change invalidates it, the time bound
    caps it. ``live_fingerprints`` is ``(every identity, the ones a current secret names)``, and None
    when the store cannot say which accounts are retired (D41)."""
    sig = _credential_signature()
    hit = _connected_cache.get(provider)
    if hit and hit[1] == sig and now.timestamp() - hit[0] < CONNECTED_CACHE_S:
        return hit[2], hit[3], hit[4]
    try:
        connected = bool(default_connected(provider))
    except Exception:  # noqa: BLE001
        logger.debug("quota connected check failed for %s", provider, exc_info=True)
        connected = False
    try:
        multi = connected and bool(default_multi_account(provider))
    except Exception:  # noqa: BLE001
        logger.debug("quota multi-account check failed for %s", provider, exc_info=True)
        multi = False
    live = None
    if connected:
        try:
            live = default_live_fingerprints(provider)
        except Exception:  # noqa: BLE001
            logger.debug("quota live-fingerprint check failed for %s", provider, exc_info=True)
    _connected_cache[provider] = (now.timestamp(), sig, connected, multi, live)
    return connected, multi, live


def _connected_cached(provider: str, now: datetime) -> bool:
    return _credential_state(provider, now)[0]


def status_groups(now: Optional[datetime] = None,
                  connected: Optional[Callable[[str], bool]] = None,
                  multi_account: Optional[Callable[[str], bool]] = None) -> list[dict]:
    """Wire groups for the status bar: one per connected provider with stored windows, never fetching.
    A multi-account pool is listed with no windows and ``multi_account: true`` (quota unknown)."""
    now = now or _utc_now()
    is_connected = connected or (lambda p: _connected_cached(p, now))
    is_multi = multi_account or (lambda p: _credential_state(p, now)[1])
    groups = []
    for provider in LABEL_ORDER:
        if not is_connected(provider):
            continue
        if is_multi(provider):
            snap = _read_raw(provider) or {}
            count = snap.get("multi_account_count")
            groups.append({"provider": provider, "fetched_at": None, "age_s": None,
                           "error": dict(MULTI_ACCOUNT_ERROR), "backoff_until": None,
                           "windows": [], "hint": None, "multi_account": True,
                           "multi_account_count": count if isinstance(count, int) else None})
            continue
        if _is_quota_aware(provider):
            # D41: a quota_aware provider owns per-account facts/views. A legacy single-account
            # snapshot (written by an earlier build) must NEVER be shown - it would present one
            # account's usage as the whole subscription's. Serve the profile view or nothing.
            view = read_view(current_profile(), provider, now=now)
            windows = [{k: w.get(k) for k in ("period", "used_percent", "reset_at", "rolled") if k in w}
                       for w in (view or {}).get("windows") or []]
            if not windows:
                continue
            groups.append({"provider": provider, "fetched_at": None, "age_s": None,
                           "error": None, "backoff_until": None, "windows": windows, "hint": None})
            continue
        snap = read_snapshot(provider, now)
        if not snap or not snap.get("windows"):
            continue
        fetched = _dt(snap.get("fetched_at"))
        err = snap.get("error")
        groups.append({
            "provider": provider,
            "fetched_at": snap.get("fetched_at"),
            "age_s": int((now - fetched).total_seconds()) if fetched else None,
            "error": {"status": err.get("status"), "message": err.get("message")} if err else None,
            "backoff_until": snap.get("backoff_until"),
            "windows": [{k: w.get(k) for k in ("period", "used_percent", "reset_at", "rolled") if k in w}
                        for w in snap["windows"]],
            "hint": None,
        })
    hint = _route_hint(groups, now)
    if hint:
        for g in groups:
            if g["provider"] == "anthropic":
                g["hint"] = hint
    return groups


_PERIOD_LABEL = {"5h": "Current session", "7d": "Current week", "monthly": "Monthly",
                 "opus 7d": "Opus week", "sonnet 7d": "Sonnet week"}


def account_snapshot(provider: str, now: Optional[datetime] = None):
    """The stored quota for ``provider`` as an ``AccountUsageSnapshot`` (for the ``/usage`` renderers),
    fetched through :func:`refresh` only when the refresh rule allows; None when nothing is stored."""
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

    name = str(provider or "").strip().lower()
    if name not in SUPPORTED:
        return None
    now = now or _utc_now()
    if _is_quota_aware(name):
        # D41: never present a legacy single-account snapshot for a quota_aware provider; the
        # profile view (or an explicit unknown) is the only source.
        view = read_view(current_profile(), name, now=now)
        windows_raw = (view or {}).get("windows") or []
        if not windows_raw:
            return None
        windows = tuple(
            AccountUsageWindow(label=str(_PERIOD_LABEL.get(w.get("period"), w.get("period")) or ""),
                               used_percent=w.get("used_percent"), reset_at=_dt(w.get("reset_at")),
                               status=w.get("status"))
            for w in windows_raw)
        return AccountUsageSnapshot(provider=name, source="quota_state", fetched_at=now, windows=windows)
    try:
        refresh(name, now=now)
    except Exception:  # noqa: BLE001
        logger.debug("quota refresh for %s failed", name, exc_info=True)
    snap = read_snapshot(name, now)
    if snap is None:
        return None
    fetched = _dt(snap.get("fetched_at")) or now
    if snap.get("multi_account"):
        return AccountUsageSnapshot(
            provider=name, source="quota_state", fetched_at=fetched, windows=(),
            unavailable_reason="several accounts in the credential pool; subscription quota unknown")
    if not snap.get("windows"):
        return None
    windows = tuple(
        AccountUsageWindow(label=_PERIOD_LABEL.get(w["period"], w["period"]), used_percent=w.get("used_percent"),
                           reset_at=_dt(w.get("reset_at")), status=w.get("status"))
        for w in snap["windows"])
    err = snap.get("error")
    details = (f"Last fetch failed ({err.get('status')}); showing data from {snap.get('fetched_at')}",) if err else ()
    return AccountUsageSnapshot(provider=name, source="quota_state", fetched_at=fetched, windows=windows,
                                details=details)


def called_providers() -> list[str]:
    """Subscriptions this process billed (``mark_called``), in a stable order."""
    return [p for p in SUPPORTED if p in _last_touch]


def refresh_in_background(providers, *, name: str = "quota-refresh") -> Optional[threading.Thread]:
    """Apply the refresh rule for ``providers`` on a daemon thread (off the caller's critical path);
    None when there is nothing to refresh."""
    targets = [p for p in dict.fromkeys(str(p or "").strip().lower() for p in providers) if p in SUPPORTED]
    if not targets:
        return None

    def _run() -> None:
        for provider in targets:
            try:
                refresh(provider)
            except Exception:  # noqa: BLE001
                logger.debug("quota refresh for %s failed", provider, exc_info=True)

    thread = threading.Thread(target=_run, name=name, daemon=True)
    thread.start()
    return thread


def bootstrap_connected(connected: Optional[Callable[[str], bool]] = None) -> dict:
    """Fetch once for every connected provider that has no snapshot yet (startup, `hermes quota`)."""
    out = {}
    for provider in SUPPORTED:
        if _read_raw(provider) is None:
            try:
                out[provider] = refresh(provider, connected=connected)
            except Exception as exc:  # noqa: BLE001
                logger.debug("quota bootstrap %s failed", provider, exc_info=True)
                out[provider] = f"error: {exc}"
    return out


# ============================================================================
# Quota-aware credential selection: per-account FACTS + per-profile VIEWS (D13, D23, D33,
# D34, D35, D41) + the QuotaRefresher (D42). Facts are shared across profiles; views are
# derived per profile and never blend accounts.
# ============================================================================

REFRESH_INTERVAL_S = 60
STALE_S = 600
REFRESH_JITTER = 0.2

_STRATEGY_QUOTA_AWARE = "quota_aware"

_UNSET = object()


def _is_quota_aware(provider: str) -> bool:
    """True when this provider's configured pool strategy is ``quota_aware``."""
    try:
        from agent.credential_pool import get_pool_strategy
        return get_pool_strategy(provider) == _STRATEGY_QUOTA_AWARE
    except Exception:
        return False


def current_profile() -> str:
    """Name of the profile this process runs as (``default`` when unset/unknown)."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "default"
    except Exception:
        return "default"


@contextlib.contextmanager
def _profile_bound(profile: str):
    """Bind the REQUESTED profile's home while its policy and strategy are resolved, so a
    cross-profile read applies THAT profile's config, never the ambient process's (D23/D33)."""
    token = None
    try:
        from hermes_cli.profiles import get_profile_dir
        from hermes_constants import set_hermes_home_override
        token = set_hermes_home_override(get_profile_dir(profile))
    except Exception:  # noqa: BLE001 — unknown/invalid name: keep the ambient home
        logger.debug("profile home binding failed for %s", profile, exc_info=True)
    try:
        yield
    finally:
        if token is not None:
            try:
                from hermes_constants import reset_hermes_home_override
                reset_hermes_home_override(token)
            except Exception:  # noqa: BLE001
                pass


def _safe_fp(fingerprint: str) -> str:
    """A fingerprint is ``sha256:...`` (contains a colon); sanitize it for a filename."""
    return str(fingerprint or "").replace(":", "_").replace("/", "_")


# ---------------------------------------------------------------- facts (shared)

def accounts_root() -> Path:
    return quota_dir() / "accounts"


def account_facts_path(provider: str, fingerprint: str) -> Path:
    return accounts_root() / provider / f"{_safe_fp(fingerprint)}.json"


def read_account_facts(provider: str, fingerprint: str) -> Optional[dict]:
    try:
        return json.loads(account_facts_path(provider, fingerprint).read_text())
    except (OSError, ValueError):
        return None


def list_account_facts(provider: str) -> list[dict]:
    out: list[dict] = []
    directory = accounts_root() / provider
    try:
        names = sorted(directory.iterdir())
    except OSError:
        return out
    for path in names:
        if path.suffix != ".json":
            continue
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return out


def write_account_facts(provider: str, fingerprint: str, data: dict) -> Path:
    """Persist one account's FACTS record (atomic). Never writes policy-shaped values."""
    path = account_facts_path(provider, fingerprint)
    _write_json_atomic(path, data)
    return path


def _merge_facts_entries(existing: Optional[dict], entries: Optional[list]) -> list:
    merged: list[dict] = list((existing or {}).get("entries") or [])
    seen = {(e.get("profile"), e.get("entry_id"), e.get("label")) for e in merged}
    for entry in entries or []:
        key = (entry.get("profile"), entry.get("entry_id"), entry.get("label"))
        if key not in seen:
            merged.append(dict(entry))
            seen.add(key)
    return merged


_facts_locks: dict[str, threading.Lock] = {}
_facts_locks_guard = threading.Lock()


@contextlib.contextmanager
def _facts_write_lock(provider: str, fingerprint: str):
    """Serialize one fingerprint's facts read-modify-write across THREADS (a process lock)
    and PROCESSES (flock), so a concurrent refresh cannot erase a recorded refusal or the
    merged ``entries`` metadata. Never held across an endpoint call (D42)."""
    key = f"{provider}/{_safe_fp(fingerprint)}"
    with _facts_locks_guard:
        tlock = _facts_locks.setdefault(key, threading.Lock())
    tlock.acquire()
    try:
        lock_path = quota_dir() / "lock" / f"facts_{_safe_fp(key)}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        tlock.release()


def store_account_facts(
    provider: str, fingerprint: str, windows: list, *, entries: Optional[list] = None,
    fetched_at: Optional[str] = None, attempted_at: Optional[str] = None,
    error: Optional[dict] = None, backoff_until: Optional[str] = None,
    metered_exhausted_until: Any = _UNSET, now: Optional[datetime] = None,
) -> Path:
    """Write the FACTS record for one credential fingerprint.

    FACTS ONLY (D23): windows + status, when it was fetched/attempted, the last error and its
    backoff, ``metered_exhausted_until``, and the pool entries seen for this fingerprint. No
    ``state`` / ``serving`` / ``states`` / history / ``metered`` - those are per-reader (D33).

    The read-modify-write runs under the per-fingerprint lock so a concurrent ``record_refusal``
    or another writer cannot be lost to a stale read (D47).
    """
    now = now or _utc_now()
    with _facts_write_lock(provider, fingerprint):
        existing = read_account_facts(provider, fingerprint) or {}
        if metered_exhausted_until is _UNSET:
            metered_exhausted_until = existing.get("metered_exhausted_until")
        elif isinstance(metered_exhausted_until, datetime):
            metered_exhausted_until = metered_exhausted_until.isoformat()
        record = {
            "provider": provider,
            "fingerprint": fingerprint,
            "entries": _merge_facts_entries(existing, entries),
            "windows": list(windows or []),
            "metered_exhausted_until": metered_exhausted_until,
            "fetched_at": fetched_at if fetched_at is not None else existing.get("fetched_at"),
            "attempted_at": attempted_at or now.isoformat(),
            "error": error,
            "backoff_until": backoff_until,
        }
        return write_account_facts(provider, fingerprint, record)


def fetch_account_facts(
    provider: str, fingerprint: str, *, entries: Optional[list] = None,
    fetcher: Optional[Callable] = None, now: Optional[datetime] = None,
) -> str:
    """Per-entry fetch API: fetch ONE credential's usage and store its FACTS record.

    Returns ``fetched`` | ``error`` | ``backoff`` | ``unsupported``. Never called on a request
    path (D11); the refresher and explicit ``--refresh`` drive it. The default fetcher is the
    provider's own usage hook, which for ``opencode-go`` honours an explicit credential.
    """
    name = str(provider or "").strip().lower()
    now = now or _utc_now()
    if name not in SUPPORTED:
        return "unsupported"
    prev = read_account_facts(name, fingerprint) or {}
    if _in_backoff(prev, now):
        return "backoff"
    entry = _pool_entry(name, fingerprint)
    if entries is None:
        entries = _entry_shape(entry)
    try:
        snapshot = (fetcher or _fetcher_for_entry(name, entry))()
    except Exception as exc:  # noqa: BLE001 — classified, never swallowed silently
        err = QuotaFetchError.from_exception(name, exc)
        wait = _backoff_wait(prev, err)
        store_account_facts(
            name, fingerprint, prev.get("windows") or [], entries=entries,
            attempted_at=now.isoformat(),
            error={"status": err.status, "message": err.message, "backoff_s": wait},
            backoff_until=(now + timedelta(seconds=wait)).isoformat(), now=now,
        )
        return "error"
    windows = _windows_from_snapshot(snapshot) if snapshot is not None else []
    store_account_facts(
        name, fingerprint, windows, entries=entries,
        fetched_at=now.isoformat(), attempted_at=now.isoformat(),
        error=None, backoff_until=None, now=now,
    )
    return "fetched"


def record_refusal(
    provider: str, fingerprint: str, *, state: str, reset_at: Any = None,
    now: Optional[datetime] = None, backoff_s: Optional[int] = None,
) -> bool:
    """Record a metered-cushion refusal (D28/D47).

    A rate-limit/quota-class refusal observed while the account was classified ``overflow``
    (it was serving as the metered cushion) sets ``metered_exhausted_until`` EVEN when the
    stored plan windows already look spent - that is the motivating case. A refusal while the
    account was classified ``covered`` is a PLAN refusal and sets nothing.
    """
    now = now or _utc_now()
    if str(state or "") != "overflow":
        return False
    with _facts_write_lock(provider, fingerprint):
        facts = read_account_facts(provider, fingerprint)
        if facts is None:
            return False
        until = _dt(reset_at) if reset_at else None
        if until is None:
            until = now + timedelta(seconds=backoff_s or BACKOFF_429_DEFAULT_S)
        facts["metered_exhausted_until"] = until.isoformat()
        write_account_facts(provider, fingerprint, facts)
        return True


# ---------------------------------------------------------------- attribution (D42)

def served_fingerprint(
    provider: Optional[str], *, api_key: Optional[str] = None,
    entry_id: Optional[str] = None, base_url: Optional[str] = None,
) -> Optional[str]:
    """The credential fingerprint of the entry that ACTUALLY served, resolved from the pool's
    real serving state (D26/D36/D42).

    ``base_url`` is never a discriminator: real opencode-go accounts share ONE base_url, so
    matching on it would silently attribute the wrong account (or, when shared, resolve to
    nothing). Resolution order is the stable ``entry_id`` (carried by the turn path), then a
    unique ``api_key`` match. When a caller DOES supply an identity that matches no unique
    account the result is ``None`` ("unknown") - the pool's current entry and single-account
    inference apply ONLY when no identity was supplied at all, so a foreign credential is
    never attributed to a configured account. Never raises.
    """
    name = quota_provider(provider, base_url)
    if name not in SUPPORTED:
        return None
    try:
        from agent.credential_pool import load_pool
        pool = load_pool(name, quota_bootstrap=False)
        entries = pool.entries()
    except Exception:
        return None
    if entry_id:
        for entry in entries:
            if entry.id == entry_id:
                return entry.extra.get("secret_fingerprint") or None
    if api_key:
        fps = {
            entry.extra.get("secret_fingerprint")
            for entry in entries
            if getattr(entry, "runtime_api_key", None) == api_key
            and entry.extra.get("secret_fingerprint")
        }
        if len(fps) == 1:
            return next(iter(fps))
        # A supplied credential that matches no pool entry (or several) is UNKNOWN: the
        # contract forbids guessing the pool's current/single account for it (D26/D42).
        return None
    if entry_id:
        # An entry id was supplied but matched no entry: unknown, never a guess.
        return None
    # No credential identity was supplied: infer from the pool's real serving state.
    try:
        current = pool.current()
    except Exception:
        current = None
    if current is not None:
        return current.extra.get("secret_fingerprint") or None
    # A single-account pool is unambiguous even before any selection has been recorded.
    known = [e.extra.get("secret_fingerprint") for e in entries if e.extra.get("secret_fingerprint")]
    if len(known) == 1:
        return known[0]
    return None


# ---------------------------------------------------------------- views (per profile)

def views_root() -> Path:
    return quota_dir() / "views"


def view_path(profile: str, provider: str) -> Path:
    return views_root() / (profile or "default") / f"{provider}.json"


def _read_view_raw(profile: str, provider: str) -> Optional[dict]:
    try:
        return json.loads(view_path(profile, provider).read_text())
    except (OSError, ValueError):
        return None


def write_view(profile: str, provider: str, view: dict) -> Path:
    path = view_path(profile, provider)
    _write_json_atomic(path, view)
    return path


def note_serving(provider: str, fingerprint: str, *, now: Optional[datetime] = None) -> None:
    """Record which account actually served into THIS profile's view (D34/D35), so the published
    windows and the history series belong to the account that served rather than a stale value."""
    if not fingerprint:
        return
    profile = current_profile()
    stored: Optional[dict] = _read_view_raw(profile, provider)
    if stored is None:
        stored = {"provider": provider}
    if stored.get("serving") == fingerprint:
        return
    stored["serving"] = fingerprint
    stored["serving_since"] = (now or _utc_now()).isoformat()
    # The series restarts on a serving change: chaining two accounts' samples is not a valid
    # time series (D35).
    stored["history"] = []
    write_view(profile, provider, stored)


def _entry_shape(entry) -> list[dict]:
    """The pool entry as it appears in a facts record (profile, entry_id, label, source), or []
    when the fingerprint resolved to no entry."""
    if entry is None:
        return []
    return [{"profile": current_profile(), "entry_id": entry.id,
             "label": entry.label, "source": entry.source}]


def _provider_profile(provider: str):
    try:
        from providers import get_provider_profile
        return get_provider_profile(provider)
    except Exception:
        return None


def _policy_for(provider: str, policy: Optional[dict] = None) -> dict:
    if isinstance(policy, dict):
        return policy
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
        blocks = config.get("credential_pool_policy")
        block = blocks.get(provider) if isinstance(blocks, dict) else None
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


@dataclass(frozen=True)
class _TargetIdentity:
    """A stored facts entry exposed to the ONE target resolver's ``.id/.label/.source`` shape.

    Used only when a read carries no live pool (the TUI/CLI snapshot path); selection reads pass
    their pool and resolve against its ordered entries instead (D33/D45).
    """

    id: Any
    label: Any
    source: Any


def _target_candidates(pool, scoped: list, profile: str) -> list:
    """The reading profile's OWN entries a ``metered_overflow`` target resolves against (D33/D45).

    The live pool's ordered entries when the read carries one - every selection path does - else
    the profile's stored facts entries adapted to the resolver's identity shape. Building ONE
    ordered list per read keeps the 1-based index form consistent with the config check.
    """
    if pool is not None:
        try:
            entries = list(pool.entries())
        except Exception:  # noqa: BLE001
            entries = []
        if entries:
            return entries
    out: list = []
    for facts in scoped:
        for entry in facts.get("entries") or []:
            if entry.get("profile") == profile:
                out.append(_TargetIdentity(
                    entry.get("entry_id") or entry.get("id"),
                    entry.get("label"), entry.get("source"),
                ))
    return out


def _declared_metered_ids(policy: dict, candidates: list) -> Optional[set]:
    """The entry ids a DECLARED ``metered_overflow`` list names, or ``None`` when it is undeclared
    (then the provider's ``bills_when_plan_spent`` capability decides, D38/D49).

    Every target is resolved with the ONE owner
    :func:`credential_pool_admin.resolve_entry_target`, so classification applies the SAME
    precedence (exact entry id > unique label > ``env:<VAR>`` source > bare VAR > 1-based index)
    as the config check. An unmatched or ambiguous target names no account: a declared list is
    authoritative (D49), so nothing is metered rather than the provider default leaking through.
    """
    targets = policy.get("metered_overflow") if isinstance(policy, dict) else None
    if not isinstance(targets, list):
        return None
    from agent.credential_pool_admin import resolve_entry_target
    ids: set = set()
    for target in targets:
        _index, entry, error = resolve_entry_target(target, candidates)
        entry_id = getattr(entry, "id", None)
        if entry is not None and not error and entry_id is not None:
            ids.add(str(entry_id))
    return ids


def _provider_metered(provider: str) -> bool:
    """Provider-default metered-ness when no ``metered_overflow`` list is declared (D38/D49)."""
    profile = _provider_profile(provider)
    return bool(getattr(profile, "bills_when_plan_spent", False))


def _read_health(facts: dict, now: datetime) -> str:
    if facts.get("error"):
        return "error"
    fetched = _dt(facts.get("fetched_at"))
    if fetched is None:
        return "stale" if facts.get("attempted_at") else "unknown"
    return "stale" if (now - fetched).total_seconds() > STALE_S else "ok"


def _profile_entry(facts: dict, profile: str) -> dict:
    """The READING profile's own entry for a shared facts record: an entry tagged with that
    profile, else {} (an untagged record carries no profile identity)."""
    for entry in facts.get("entries") or []:
        if entry.get("profile") == profile:
            return entry
    return {}


def _pick_serving(facts_list: list, by_fp: dict, stored_serving, pool, now: datetime, billed=()):
    fps = [f.get("fingerprint") for f in facts_list if f.get("fingerprint")]
    if stored_serving in fps:
        return stored_serving
    order = fps
    if pool is not None:
        try:
            priority = {
                e.extra.get("secret_fingerprint"): e.priority for e in pool.entries()
            }
            order = sorted(fps, key=lambda fp: priority.get(fp, 1 << 30))
        except Exception:
            order = fps
    covered = [fp for fp in order if by_fp.get(fp, {}).get("state") == "covered"]
    # D41: a name the credential used before a key change is still a live identity of the account,
    # but the account is SERVED under the identity its credential bills with today.
    current = [fp for fp in order if fp in billed]
    for candidates in (current, order):
        picked = [fp for fp in candidates if fp in covered] or candidates
        if picked:
            return picked[0]
    return None


def _live_accounts(provider: str, pool, now: datetime) -> tuple[Optional[set], set]:
    """``(every identity the READING profile's live credentials answer to for ``provider``, the ones
    their CURRENT secrets name)`` (D41): the live pool when the read carries one - every selection
    path does - else the local credential state, which the status bar already reads each turn. An
    unknown membership is ``(None, set())``: nothing may be hidden on a guess."""
    identities = None
    if pool is not None:
        try:
            from agent.account_usage import live_entry_identities
            identities = live_entry_identities(pool.entries())
        except Exception:  # noqa: BLE001
            logger.debug("quota live accounts from the pool failed for %s", provider, exc_info=True)
    else:
        identities = _credential_state(provider, now)[2]
    return identities if identities else (None, set())


def _compose_view(
    profile: str, provider: str, stored: Optional[dict], *, now: datetime,
    policy: Optional[dict], pool,
) -> dict:
    with _profile_bound(profile):
        policy = _policy_for(provider, policy)
        strategy = _STRATEGY_QUOTA_AWARE if _is_quota_aware(provider) else "fill_first"
        # Resolved under the reading profile's own home: a cross-profile read must filter by THAT
        # profile's live credentials, not the ambient process's (D23/D33/D41).
        live, billed = _live_accounts(provider, pool, now)
    accounts: list[dict] = []
    states = {"covered": 0, "overflow": 0, "exhausted": 0}
    by_fp: dict = {}
    scoped: list[dict] = []
    for facts in list_account_facts(provider):
        entries = facts.get("entries") or []
        mine = [e for e in entries if e.get("profile") == profile]
        # D33: an account belongs to the reading profile only when THAT profile's pool owns
        # it. A record tagged solely with other profiles is theirs, not this profile's; an
        # untagged record has no known owner and stays visible.
        if entries and not mine:
            continue
        # D41: ownership is current, not historical. A fingerprint missing from the live pool is a
        # credential that was rotated away - its facts stay on disk for whoever else owns them, but
        # this profile must not paint them as its own usage, and the view file cannot grow with every
        # key the profile has ever used.
        if live is not None:
            fingerprint = facts.get("fingerprint")
            if fingerprint is not None and fingerprint not in live:
                continue
        scoped.append(facts)
    # D45: resolve the DECLARED metered_overflow list ONCE, with the ONE target resolver, against
    # the reading profile's OWN entries - the same precedence the config check applies - then mark
    # an account metered by membership. A declared-but-unmatched list names no account (D49), and
    # an undeclared key falls back to the provider's billing capability.
    metered_ids = _declared_metered_ids(policy, _target_candidates(pool, scoped, profile))
    for facts in scoped:
        fp = facts.get("fingerprint")
        mine = [e for e in (facts.get("entries") or []) if e.get("profile") == profile]
        entry = mine[0] if mine else {}
        windows = [_rolled(w, now) for w in facts.get("windows") or []]
        # D23: every policy-shaped value is derived HERE, at read time, from THIS profile's
        # CURRENT config + the stored facts - never reused from a previously-written view.
        # Editing ``credential_pool_policy`` must change the next read with no rewrite, so a
        # stored ``metered`` is a pure cache for display, not an input to classification. Two
        # profiles with different policies therefore read different states from the SAME facts.
        if metered_ids is None:
            metered = _provider_metered(provider)
        else:
            entry_id = entry.get("entry_id")
            metered = entry_id is not None and str(entry_id) in metered_ids
        state = qstate.account_state(
            windows, metered=metered,
            metered_exhausted_until=facts.get("metered_exhausted_until"), now=now,
        )
        fetched = _dt(facts.get("fetched_at"))
        record = {
            "fingerprint": fp,
            "label": entry.get("label"),
            "entry_id": entry.get("entry_id"),
            "state": state,
            "metered": metered,
            "read_health": _read_health(facts, now),
            "windows": windows,
            "fetched_at": facts.get("fetched_at"),
            "age_s": int((now - fetched).total_seconds()) if fetched else None,
            "attempted_at": facts.get("attempted_at"),
            "last_error": (facts.get("error") or {}).get("message"),
            "backoff_until": facts.get("backoff_until"),
            "recovery_at": qstate.effective_recovery(
                windows, facts.get("metered_exhausted_until"), now
            ).isoformat(),
        }
        accounts.append(record)
        if fp is not None:
            by_fp[fp] = record
        if state in states:
            states[state] += 1
    serving = _pick_serving(scoped, by_fp, (stored or {}).get("serving"), pool, now, billed)
    previous_serving = (stored or {}).get("serving")
    history = list((stored or {}).get("history") or [])
    serving_since = (stored or {}).get("serving_since")
    if previous_serving != serving or serving_since is None:
        # The series restarts when serving changes: chaining two accounts' samples is not a
        # valid time series (D35).
        history = []
        serving_since = now.isoformat()
    return {
        "provider": provider,
        "strategy": strategy,
        "serving": serving,
        "serving_since": serving_since,
        "windows": by_fp.get(serving, {}).get("windows", []),
        "history": history,
        "accounts": accounts,
        "states": states,
    }


def read_view(
    profile: str, provider: str, *, now: Optional[datetime] = None,
    policy: Optional[dict] = None, pool=None,
) -> Optional[dict]:
    """The ONE profile-scoped reader (D41) used by selection, the TUI snapshot and the CLI.

    Returns the derived view for a provider on ``quota_aware`` (or one that already has a view
    file), with per-account state recomputed at read time FROM THIS PROFILE'S CURRENT CONFIG
    (D23): a policy edit is visible on the very next read, with no rewrite. The REQUESTED
    profile's home is bound while its policy and strategy are read, so a cross-profile read
    applies that profile's config rather than the ambient one (D33). Returns None for any
    other provider so the caller falls back to the legacy ``snapshot/<provider>.json`` path
    unchanged (D33).
    """
    name = str(provider or "").strip().lower()
    if name not in SUPPORTED:
        return None
    now = now or _utc_now()
    with _profile_bound(profile):
        quota_aware = _is_quota_aware(name)
    stored = _read_view_raw(profile, name)
    if stored is None and not quota_aware:
        return None
    return _compose_view(profile, name, stored, now=now, policy=policy, pool=pool)


def serving_account(view: Optional[dict]) -> dict:
    """The account a view serves: ``serving`` while the view still holds that account, else its first.

    Both readers (the CLI subscription group and the gateway capacity group) build their provider
    line from it, so the two never disagree about whose windows the line carries (D41).
    """
    accounts = list((view or {}).get("accounts") or [])
    by_fingerprint = {account.get("fingerprint"): account for account in accounts}
    return by_fingerprint.get((view or {}).get("serving")) or (accounts[0] if accounts else {})


def update_view(
    profile: str, provider: str, *, now: Optional[datetime] = None,
    policy: Optional[dict] = None, pool=None,
) -> dict:
    """Recompute the per-profile view from facts + this profile's policy, record a history
    sample when the SERVED data has changed, and persist it (D41).

    ``load_pool(quota_bootstrap=True)`` calls this on every billed aux call, so two bounds keep
    the view file from growing on the request path: a sample is appended only when the serving
    account's fingerprint or windows differ from the last sample (an unchanged recompose adds
    nothing), and the series is capped at ``VIEW_HISTORY_MAX_SAMPLES`` (D35)."""
    name = str(provider or "").strip().lower()
    now = now or _utc_now()
    stored = _read_view_raw(profile, name)
    view = _compose_view(profile, name, stored, now=now, policy=policy, pool=pool)
    if view.get("serving"):
        sample = {
            "t": now.isoformat(),
            "fingerprint": view["serving"],
            "windows": view.get("windows") or [],
        }
        history = list(view.get("history") or [])
        last = history[-1] if history else {}
        if (last.get("fingerprint") != sample["fingerprint"]
                or last.get("windows") != sample["windows"]):
            history.append(sample)
        view["history"] = history[-VIEW_HISTORY_MAX_SAMPLES:]
    write_view(profile, name, view)
    return view


# ---------------------------------------------------------------- refresher (D42)

_pending_lock = threading.Lock()
_pending_refreshes: set = set()
_refresher: Optional["QuotaRefresher"] = None
_refresher_guard = threading.Lock()
# The profile context each queued account must be fetched under, keyed like the queue. A bare
# worker thread has no ContextVars of the enqueuing turn, so the home/secret scope is copied at
# enqueue time; without it a fingerprint queued by another profile resolves in the launch
# profile and fetches the wrong (or a default) account (D42).
_pending_context: dict = {}


@dataclass(frozen=True)
class _RefreshScope:
    home: Optional[str]
    secrets: Optional[dict]


def _capture_scope() -> "_RefreshScope":
    """Copy the enqueuing turn's home + secret scope so the worker can re-bind them."""
    home: Optional[str] = None
    try:
        from hermes_constants import get_hermes_home, get_hermes_home_override
        home = get_hermes_home_override() or str(get_hermes_home())
    except Exception:  # noqa: BLE001
        logger.debug("refresh scope home capture failed", exc_info=True)
    secrets: Optional[dict] = None
    try:
        from agent.secret_scope import current_secret_scope
        scope = current_secret_scope()
        secrets = dict(scope) if scope else None
    except Exception:  # noqa: BLE001
        logger.debug("refresh scope secret capture failed", exc_info=True)
    return _RefreshScope(home=home, secrets=secrets)


@contextlib.contextmanager
def _bind_scope(scope: Optional["_RefreshScope"]):
    """Re-bind a captured enqueue scope around a fetch in the worker thread."""
    home_token = secret_token = None
    if scope is not None:
        try:
            if scope.home:
                from hermes_constants import set_hermes_home_override
                home_token = set_hermes_home_override(scope.home)
            from agent.secret_scope import set_secret_scope
            secret_token = set_secret_scope(scope.secrets)
        except Exception:  # noqa: BLE001
            logger.debug("refresh scope binding failed", exc_info=True)
    try:
        yield
    finally:
        if secret_token is not None:
            try:
                from agent.secret_scope import reset_secret_scope
                reset_secret_scope(secret_token)
            except Exception:  # noqa: BLE001
                pass
        if home_token is not None:
            try:
                from hermes_constants import reset_hermes_home_override
                reset_hermes_home_override(home_token)
            except Exception:  # noqa: BLE001
                pass


def enqueue_refresh(provider: str, fingerprint: Optional[str] = None) -> None:
    """Queue a ``(provider, fingerprint)`` refresh with the enqueuing profile's scope; never
    fetches here (D11/D42)."""
    name = str(provider or "").strip().lower()
    if name not in SUPPORTED:
        return
    scope = _capture_scope()
    with _pending_lock:
        _pending_refreshes.add((name, fingerprint))
        _pending_context[(name, fingerprint)] = scope


def pending_refreshes() -> set:
    """Snapshot of queued ``(provider, fingerprint)`` refresh requests (test/observability)."""
    with _pending_lock:
        return set(_pending_refreshes)


def _drain_pending() -> list:
    with _pending_lock:
        items = list(_pending_refreshes)
        _pending_refreshes.clear()
        contexts = dict(_pending_context)
        _pending_context.clear()
    return [(provider, fingerprint, contexts.get((provider, fingerprint)))
            for provider, fingerprint in items]


class QuotaRefresher:
    """One lightweight thread owned by the pool; drains the ``(provider, fingerprint)`` queue.

    Never entered from a request path (D11/D42): callers only ever enqueue. Accounts in endpoint
    backoff are skipped by the fetch's own backoff arithmetic; their staleness is visible via
    ``age_s``/``read_health``.
    """

    def __init__(self, *, interval_s: int = REFRESH_INTERVAL_S, jitter: float = REFRESH_JITTER,
                 fetcher: Optional[Callable] = None) -> None:
        self.interval_s = interval_s
        self.jitter = jitter
        self._fetcher = fetcher
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # Set (under ``_lock``) the instant the worker commits to leaving its loop, so a
        # ``start()`` racing a timed-out stop starts a replacement rather than reviving a
        # worker that is already exiting (D42).
        self._exiting = False

    def _sleep_interval(self) -> float:
        spread = self.interval_s * self.jitter
        return max(0.05, self.interval_s + random.uniform(-spread, spread))

    def start(self) -> "QuotaRefresher":
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive() and not self._exiting:
                # A stop() that timed out (an in-flight fetch outlasts the join) left the worker
                # alive but with ``_stop`` set; clearing it lets that same worker resume its loop
                # instead of dying, so a live owner is never left without a refresher (D42). A
                # worker that has already committed to exiting does no further work, so it is
                # replaced instead of revived (one working worker either way).
                self._stop.clear()
                return self
            self._stop.clear()
            self._exiting = False
            self._thread = threading.Thread(target=self._run, name="quota-refresher", daemon=True)
            self._thread.start()
        return self

    def request_stop(self) -> None:
        """Signal the worker to leave WITHOUT waiting for it. The caller joins through
        :meth:`wait_stopped` after dropping the lifecycle guard, so a worker-side pool finalizer
        that needs that guard can never deadlock against this stop (D42)."""
        with self._lock:
            self._stop.set()

    def wait_stopped(self, timeout: float = 2.0) -> None:
        with self._lock:
            thread = self._thread
        # A finalizer may collect the last pool ON the worker thread; a thread cannot join
        # itself, and it is already stopping, so leave the handle to clear on the next start.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            with self._lock:
                # Retain the handle until the worker has TRULY exited, so a start() that races
                # a timed-out stop cannot overlap a second worker.
                if self._thread is thread and not thread.is_alive():
                    self._thread = None

    def stop(self, timeout: float = 2.0) -> None:
        self.request_stop()
        self.wait_stopped(timeout)

    def _exit_pending(self) -> bool:
        """Under ``_lock``: True once a stop is pending, recording that this worker is leaving
        so a racing ``start()`` replaces it rather than reviving it (D42)."""
        with self._lock:
            if self._stop.is_set():
                self._exiting = True
                return True
            return False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        # The drain cadence IS the interval: an enqueue only marks work for the NEXT drain, it
        # never shortens the current one, so a burst of billed calls cannot turn the periodic
        # drain into a fetch storm (D42).
        while not self._exit_pending():
            self._stop.wait(timeout=self._sleep_interval())
            if self._exit_pending():
                return
            for provider, fingerprint, scope in _drain_pending():
                if fingerprint is None:
                    continue
                try:
                    with _bind_scope(scope):
                        fetch_account_facts(provider, fingerprint, fetcher=self._fetcher)
                except Exception:  # noqa: BLE001
                    logger.debug("quota refresh %s/%s failed", provider, fingerprint, exc_info=True)


# Holds on the process-wide refresher: the thread runs while ANY pool holds it and stops once the
# last hold is released, so one pool's teardown never kills another's refreshing (D42). A hold ends
# on an explicit release AND when its pool is garbage collected, so a pool nobody keeps does not
# leave an orphan daemon fetching forever. ``_owner_tokens`` is the authoritative set of live
# holds; ``_pool_owner_token`` maps a pool to the token it was given and ``_owner_finalizers`` maps
# a token to its finalizer. A release must NOT enumerate those indexes: holding another live pool
# just to delete its entry drops that pool's last reference on the releasing thread, and the
# finalizer that then runs re-enters the non-reentrant ``_refresher_guard`` this release holds and
# deadlocks it. A token that has left the live set is left in the index, where ``start_refresher``
# reads it as unregistered and takes a fresh one (D42).
_pool_owner_token: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_owner_tokens: set = set()
_owner_finalizers: dict = {}
_owner_token_seq = itertools.count(1)


def _drop_owner(token: int):
    """Under ``_refresher_guard``: drop ``token`` from the live holds and return its finalizer
    (undetached), leaving the pool index entry alone -- see the note above (D42)."""
    _owner_tokens.discard(token)
    return _owner_finalizers.pop(token, None)


def _release_owner(token: int, timeout: float = 2.0) -> None:
    """Drop one pool's hold on the refresher; stop the worker once no owner remains."""
    global _refresher
    with _refresher_guard:
        if token not in _owner_tokens:
            return
        finalizer = _drop_owner(token)
        if _owner_tokens:
            return
        refresher = _refresher
        # Signal the stop under the SAME guard that serializes registration+start(): a registering
        # owner then either still sees this hold (so no stop happens at all) or reaches its start()
        # after the signal, where start() clears the pending stop and keeps the worker alive.
        # Signalling outside this guard would let a registration land in between, and this release
        # would then kill the worker that new owner had just started (D42). The join stays outside
        # the guard so a worker-side pool finalizer waiting for that guard cannot deadlock.
        if refresher is not None:
            refresher.request_stop()
    if finalizer is not None:
        finalizer.detach()
    if refresher is not None:
        refresher.wait_stopped(timeout)
        with _refresher_guard:
            # Re-check under the guard, which also serializes every registration+start(): an
            # owner that registered while this stop ran has already started this refresher, so
            # clearing it here would strand the worker that owner just started (D42).
            if _refresher is refresher and not refresher.running and not _owner_tokens:
                _refresher = None


def start_refresher(*, pool=None, interval_s: int = REFRESH_INTERVAL_S,
                    fetcher: Optional[Callable] = None) -> QuotaRefresher:
    """Start (or return) the process-wide ``QuotaRefresher``. ``pool`` registers a live owner;
    the thread stops only once every registered pool has released it (explicitly or by being
    collected). Idempotent."""
    global _refresher
    with _refresher_guard:
        # A pool holds the refresher iff its token is still a live hold: an index entry left behind
        # by an earlier release must not suppress this registration, or the pool would keep the
        # worker running with no hold to release and its teardown would never stop it (D42).
        if pool is not None and _pool_owner_token.get(pool) not in _owner_tokens:
            token = next(_owner_token_seq)
            _pool_owner_token[pool] = token
            _owner_tokens.add(token)
            _owner_finalizers[token] = weakref.finalize(pool, _release_owner, token)
        if _refresher is None:
            _refresher = QuotaRefresher(interval_s=interval_s, fetcher=fetcher)
        # Register the owner and start the worker as ONE step under the guard, so a concurrent
        # last-owner release can never clear ``_refresher`` between this owner's registration
        # and its ``start()`` - which would leave the new owner with no worker and start an
        # orphan the process-wide stop can no longer reach (D42).
        return _refresher.start()


def stop_refresher(timeout: float = 2.0, *, pool=None) -> None:
    """Release ``pool``'s hold on the refresher; the thread stops once no pool holds it. With no
    ``pool`` this is a forced process-wide stop: it ends every hold, including one taken while it
    ran, and stops the worker."""
    global _refresher
    if pool is not None:
        with _refresher_guard:
            token = _pool_owner_token.get(pool)
        if token is None:
            return
        _release_owner(token, timeout)
        return
    with _refresher_guard:
        tokens = list(_owner_tokens)
    for token in tokens:
        _release_owner(token, timeout)
    with _refresher_guard:
        # End the force as ONE lifecycle step: holds still standing — including one registered
        # while the releases above ran — end here, together with the stop signal, so no hold can be
        # left pointing at a stopped refresher and no worker can outlive the stop (D42). A pool
        # that starts again afterwards takes a fresh hold and a fresh worker.
        for token in list(_owner_tokens):
            finalizer = _drop_owner(token)
            if finalizer is not None:
                finalizer.detach()
        refresher = _refresher
        if refresher is not None:
            refresher.request_stop()
    if refresher is not None:
        refresher.wait_stopped(timeout)
        with _refresher_guard:
            if _refresher is refresher and not refresher.running and not _owner_tokens:
                _refresher = None



def is_refresher_running() -> bool:
    refresher = _refresher
    return bool(refresher is not None and refresher.running)
