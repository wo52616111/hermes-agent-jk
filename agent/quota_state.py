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

import fcntl
import json
import logging
import os
import queue
import random
import threading
import time
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
    """Record that a billed API call just went to ``provider`` (throttled, never raises).

    The served entry's credential ``fingerprint`` (D42 attribution) is carried by all three
    billed-call paths. Callers pass whatever they cheaply know (``api_key`` / ``entry_id`` /
    ``base_url``); when none resolves to a fingerprint the call is still marked dirty.
    """
    name = quota_provider(provider, base_url)
    if name not in SUPPORTED:
        return
    ts = (now or _utc_now()).timestamp()
    last = _last_touch.get(name, -1e18)
    if ts - last < DIRTY_THROTTLE_S:
        # A mark older than the latest fetch is spent; skipping this call would lose it.
        fetched = _dt((_read_raw(name) or {}).get("fetched_at"))
        if fetched is None or last > fetched.timestamp():
            return
    _last_touch[name] = ts
    try:
        path = _path("dirty", name)
        _touch(path)
        if now is not None:
            os.utime(path, (ts, ts))
    except OSError:
        logger.debug("quota dirty mark failed for %s", name, exc_info=True)
    # Attribution: name the account that actually served so the refresher drains the right
    # (provider, fingerprint) queue entry (D42).
    resolved = fingerprint
    if resolved is None:
        resolved = served_fingerprint(name, api_key=api_key, entry_id=entry_id, base_url=base_url)
    if resolved:
        enqueue_refresh(name, resolved)


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


def _decide(provider: str, now: datetime, connected: Callable[[], bool]) -> str:
    """``connected`` is only evaluated when a fetch would happen (it may resolve credentials)."""
    if provider not in SUPPORTED:
        return "unsupported"
    snap = _read_raw(provider)
    if snap is None or snap.get("multi_account"):
        return "fetch" if connected() else "not_connected"
    backoff = _dt(snap.get("backoff_until"))
    if backoff and now < backoff:
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


def _account_fetcher(provider: str, fingerprint: str) -> Callable:
    """Resolve the pool entry carrying *fingerprint* and fetch with ITS credential, so a
    per-entry refresh reads the right account's usage; falls back to the provider fetcher."""
    try:
        from agent.credential_pool import load_pool
        for entry in load_pool(provider).entries():
            if entry.extra.get("secret_fingerprint") == fingerprint:
                api_key = entry.runtime_api_key
                base_url = entry.runtime_base_url
                return lambda: default_fetcher_for(provider, api_key=api_key, base_url=base_url)
    except Exception:  # noqa: BLE001
        logger.debug("per-entry fetcher resolution failed for %s/%s", provider, fingerprint, exc_info=True)
    return lambda: default_fetcher(provider)


def default_connected(provider: str) -> bool:
    from agent.account_usage import provider_quota_connected
    return provider_quota_connected(provider)


def default_multi_account(provider: str) -> bool:
    # A quota_aware provider keeps per-account facts/views; the legacy "collapse to one
    # unknown window list" must never apply to it (D41).
    if _is_quota_aware(provider):
        return False
    from agent.account_usage import provider_quota_multi_account
    return provider_quota_multi_account(provider)


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
    _write_json_atomic(_path("snapshot", provider, ".json"), {
        "provider": provider, "fetched_at": None, "attempted_at": now.isoformat(),
        "windows": [], "error": None, "backoff_until": None, "multi_account": True,
    })


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
        if err.status == 429:
            wait = min(BACKOFF_CAP_S, err.retry_after_s or BACKOFF_429_DEFAULT_S)
        else:
            prev_wait = int((prev.get("error") or {}).get("backoff_s") or 0)
            wait = min(BACKOFF_ERR_CAP_S, prev_wait * 2 if prev_wait else BACKOFF_ERR_START_S)
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
    return {**window, "used_percent": 0.0, "reset_at": reset.isoformat(), "rolled": True}


def read_snapshot(provider: str, now: Optional[datetime] = None) -> Optional[dict]:
    """Snapshot as stored, with windows past their reset rolled forward to 0%."""
    snap = _read_raw(provider)
    if snap is None:
        return None
    now = now or _utc_now()
    return {**snap, "windows": [_rolled(w, now) for w in snap.get("windows") or []]}


def read_all(now: Optional[datetime] = None) -> list[dict]:
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


_connected_cache: dict[str, tuple[float, tuple, bool, bool]] = {}
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


def _credential_state(provider: str, now: datetime) -> tuple[bool, bool]:
    """``(connected, multi_account)`` from local credentials (no network), cached because the status
    bar reads every turn; a credential file change invalidates it, the time bound caps it."""
    sig = _credential_signature()
    hit = _connected_cache.get(provider)
    if hit and hit[1] == sig and now.timestamp() - hit[0] < CONNECTED_CACHE_S:
        return hit[2], hit[3]
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
    _connected_cache[provider] = (now.timestamp(), sig, connected, multi)
    return connected, multi


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
            groups.append({"provider": provider, "fetched_at": None, "age_s": None,
                           "error": dict(MULTI_ACCOUNT_ERROR), "backoff_until": None,
                           "windows": [], "hint": None, "multi_account": True})
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
                           reset_at=_dt(w.get("reset_at")))
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
VIEW_TTL_S = REFRESH_INTERVAL_S
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
    """
    now = now or _utc_now()
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

    Returns ``fetched`` | ``error`` | ``unsupported``. Never called on a request path (D11);
    the refresher and explicit ``--refresh`` drive it. The default fetcher is the provider's
    own usage hook, which for ``opencode-go`` honours an explicit credential.
    """
    name = str(provider or "").strip().lower()
    now = now or _utc_now()
    if name not in SUPPORTED:
        return "unsupported"
    prev = read_account_facts(name, fingerprint) or {}
    try:
        snapshot = (fetcher or _account_fetcher(name, fingerprint))()
    except Exception as exc:  # noqa: BLE001 — classified, never swallowed silently
        err = QuotaFetchError.from_exception(name, exc)
        if err.status == 429:
            wait = min(BACKOFF_CAP_S, err.retry_after_s or BACKOFF_429_DEFAULT_S)
        else:
            prev_wait = int(((prev.get("error") or {}) or {}).get("backoff_s") or 0)
            wait = min(BACKOFF_ERR_CAP_S, prev_wait * 2 if prev_wait else BACKOFF_ERR_START_S)
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
    """Record a metered-cushion refusal (D28/D47, round-5 unified rule).

    A rate-limit/quota-class refusal observed while the account was classified ``overflow``
    (it was serving as the metered cushion) sets ``metered_exhausted_until`` EVEN when the
    stored plan windows already look spent - that is the motivating case. A refusal while the
    account was classified ``covered`` is a PLAN refusal and sets nothing.
    """
    now = now or _utc_now()
    if str(state or "") != "overflow":
        return False
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

    ``base_url`` is deliberately NOT a discriminator: real opencode-go accounts share ONE
    base_url, so matching on it would silently attribute the wrong account (or, when shared,
    resolve to nothing). Resolution order is the stable ``entry_id`` (carried by the turn
    path), then a unique ``api_key`` match, then the pool's current entry - the one the
    session is bound to / last leased. When nothing resolves a UNIQUE account the result is
    ``None`` ("unknown"): a caller must record no fingerprint rather than guess a wrong one.
    Never raises.
    """
    name = quota_provider(provider, base_url)
    if name not in SUPPORTED:
        return None
    try:
        from agent.credential_pool import load_pool
        pool = load_pool(name)
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
    # The pool's real serving state: the entry the session is currently bound to.
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


def _target_matches(target: Any, entry: dict) -> bool:
    raw = str(target or "").strip()
    if not raw:
        return False
    label = str(entry.get("label") or "")
    return raw in {
        str(entry.get("entry_id") or ""), label, f"env:{label}",
        str(entry.get("source") or ""),
    }


def _is_metered_facts(facts: dict, provider: str, policy: dict) -> bool:
    """D38: metered when a configured target matches, OR when the provider bills after the
    plan is spent."""
    targets = policy.get("metered_overflow") if isinstance(policy, dict) else None
    if isinstance(targets, list):
        for entry in facts.get("entries") or []:
            if any(_target_matches(t, entry) for t in targets):
                return True
    profile = _provider_profile(provider)
    return bool(getattr(profile, "bills_when_plan_spent", False))


def _read_health(facts: dict, now: datetime) -> str:
    if facts.get("error"):
        return "error"
    fetched = _dt(facts.get("fetched_at"))
    if fetched is None:
        return "stale" if facts.get("attempted_at") else "unknown"
    return "stale" if (now - fetched).total_seconds() > STALE_S else "ok"


def _first_entry(facts: dict) -> dict:
    entries = facts.get("entries") or []
    return entries[0] if entries else {}


def _pick_serving(facts_list: list, by_fp: dict, stored_serving, pool, now: datetime):
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
    return (covered or order or [None])[0]


def _compose_view(
    profile: str, provider: str, stored: Optional[dict], *, now: datetime,
    policy: Optional[dict], pool,
) -> dict:
    policy = _policy_for(provider, policy)
    accounts: list[dict] = []
    states = {"covered": 0, "overflow": 0, "exhausted": 0}
    by_fp: dict = {}
    facts_list = list_account_facts(provider)
    for facts in facts_list:
        fp = facts.get("fingerprint")
        windows = [_rolled(w, now) for w in facts.get("windows") or []]
        # D23: every policy-shaped value is derived HERE, at read time, from THIS profile's
        # CURRENT config + the stored facts - never reused from a previously-written view.
        # Editing ``credential_pool_policy`` must change the next read with no rewrite, so a
        # stored ``metered`` is a pure cache for display, not an input to classification. Two
        # profiles with different policies therefore read different states from the SAME facts.
        metered = _is_metered_facts(facts, provider, policy)
        state = qstate.account_state(
            windows, metered=metered,
            metered_exhausted_until=facts.get("metered_exhausted_until"), now=now,
        )
        fetched = _dt(facts.get("fetched_at"))
        entry = _first_entry(facts)
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
    serving = _pick_serving(facts_list, by_fp, (stored or {}).get("serving"), pool, now)
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
        "strategy": _STRATEGY_QUOTA_AWARE if _is_quota_aware(provider) else "fill_first",
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
    (D23): a policy edit is visible on the very next read, with no rewrite. Returns None for any
    other provider so the caller falls back to the legacy ``snapshot/<provider>.json`` path
    unchanged (D33).
    """
    name = str(provider or "").strip().lower()
    if name not in SUPPORTED:
        return None
    now = now or _utc_now()
    stored = _read_view_raw(profile, name)
    if stored is None and not _is_quota_aware(name):
        return None
    return _compose_view(profile, name, stored, now=now, policy=policy, pool=pool)


def update_view(
    profile: str, provider: str, *, now: Optional[datetime] = None,
    policy: Optional[dict] = None, pool=None,
) -> dict:
    """Recompute the per-profile view from facts + this profile's policy, append a history
    sample for the current serving account, and persist it (D41)."""
    name = str(provider or "").strip().lower()
    now = now or _utc_now()
    stored = _read_view_raw(profile, name)
    view = _compose_view(profile, name, stored, now=now, policy=policy, pool=pool)
    if view.get("serving"):
        view["history"] = list(view.get("history") or []) + [{
            "t": now.isoformat(),
            "fingerprint": view["serving"],
            "windows": view.get("windows") or [],
        }]
    write_view(profile, name, view)
    return view


# ---------------------------------------------------------------- refresher (D42)

_pending_lock = threading.Lock()
_pending_refreshes: set = set()
_refresher: Optional["QuotaRefresher"] = None
_refresher_guard = threading.Lock()


def enqueue_refresh(provider: str, fingerprint: Optional[str] = None) -> None:
    """Queue a ``(provider, fingerprint)`` refresh; never fetches here (D11/D42)."""
    name = str(provider or "").strip().lower()
    if name not in SUPPORTED:
        return
    with _pending_lock:
        _pending_refreshes.add((name, fingerprint))
    refresher = _refresher
    if refresher is not None:
        refresher.wake()


def pending_refreshes() -> set:
    """Snapshot of queued ``(provider, fingerprint)`` refresh requests (test/observability)."""
    with _pending_lock:
        return set(_pending_refreshes)


def _drain_pending() -> set:
    with _pending_lock:
        items = set(_pending_refreshes)
        _pending_refreshes.clear()
    return items


class QuotaRefresher:
    """One lightweight thread owned by the pool; drains the ``(provider, fingerprint)`` queue.

    Started lazily with the pool and stopped with it. Never entered from a request path
    (D11/D42): callers only ever enqueue. Accounts in endpoint backoff are skipped by the
    fetcher's own backoff arithmetic; their staleness is visible via ``age_s``/``read_health``.
    """

    def __init__(self, *, interval_s: int = REFRESH_INTERVAL_S, jitter: float = REFRESH_JITTER,
                 fetcher: Optional[Callable] = None) -> None:
        self.interval_s = interval_s
        self.jitter = jitter
        self._fetcher = fetcher
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _sleep_interval(self) -> float:
        spread = self.interval_s * self.jitter
        return max(0.05, self.interval_s + random.uniform(-spread, spread))

    def start(self) -> "QuotaRefresher":
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(target=self._run, name="quota-refresher", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            self._thread = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self._sleep_interval())
            self._wake.clear()
            if self._stop.is_set():
                break
            for provider, fingerprint in _drain_pending():
                if fingerprint is None:
                    continue
                try:
                    fetch_account_facts(provider, fingerprint, fetcher=self._fetcher)
                except Exception:  # noqa: BLE001
                    logger.debug("quota refresh %s/%s failed", provider, fingerprint, exc_info=True)


def start_refresher(*, interval_s: int = REFRESH_INTERVAL_S, fetcher: Optional[Callable] = None) -> QuotaRefresher:
    """Start (or return) the process-wide ``QuotaRefresher``. Idempotent."""
    global _refresher
    with _refresher_guard:
        if _refresher is None:
            _refresher = QuotaRefresher(interval_s=interval_s, fetcher=fetcher)
        refresher = _refresher
    return refresher.start()


def stop_refresher(timeout: float = 2.0) -> None:
    """Stop the process-wide refresher (pool teardown)."""
    global _refresher
    with _refresher_guard:
        refresher = _refresher
        _refresher = None
    if refresher is not None:
        refresher.stop(timeout)


def is_refresher_running() -> bool:
    refresher = _refresher
    return bool(refresher is not None and refresher.running)
