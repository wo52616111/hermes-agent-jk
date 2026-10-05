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
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

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

# Display label -> canonical period; window length by period.
_PERIOD = {
    "session": "5h", "current session": "5h", "5h": "5h", "rolling window": "5h",
    "weekly": "7d", "current week": "7d", "7d": "7d",
    "monthly": "monthly",
    "opus week": "opus 7d", "sonnet week": "sonnet 7d",
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
                base_url: Optional[str] = None) -> None:
    """Record that a billed API call just went to ``provider`` (throttled, never raises)."""
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
        period = _PERIOD.get(str(w.label).strip().lower())
        if not period or w.used_percent is None:
            continue
        reset = w.reset_at
        seconds = _SECONDS.get(period)
        if period == "monthly" and reset is not None:
            seconds = int((reset - _month_before(reset)).total_seconds())
        out.append({"period": period, "used_percent": float(w.used_percent),
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


def default_connected(provider: str) -> bool:
    from agent.account_usage import provider_quota_connected
    return provider_quota_connected(provider)


def default_multi_account(provider: str) -> bool:
    from agent.account_usage import provider_quota_multi_account
    return provider_quota_multi_account(provider)


MULTI_ACCOUNT_ERROR = {"status": 0, "message": "multi_account"}


def _store_multi_account(provider: str, now: datetime) -> None:
    """A pool of several accounts has no single quota; store it as unknown (no windows) so
    routers reading the snapshot never mistake one account's usage for the subscription's."""
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
