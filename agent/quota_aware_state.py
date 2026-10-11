"""Pure per-account quota state derivation (spec D9, D10, D25, D28, D47).

Facts (``accounts/<provider>/<fingerprint>.json``) store raw windows; every derived,
policy-shaped value (``state``, ``read_health``, ``serving``) is recomputed here on every read
so a spent window whose reset has passed rolls forward on the clock alone (D9) and a recorded
cushion refusal outranks stale plan evidence (D47).

Nothing in this module reads or writes files or the network: it is the single pure boundary
the selection path, the view writer and the tests share. Windows may be plain dicts (the stored
facts shape) or :class:`agent.account_usage.AccountUsageWindow`; both are accepted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

SPENT_ABOVE_PERCENT = 100.0

# The provider's own marker that a window is spent. Outranks ``used_percent`` (D2/D25).
SPENT_STATUS = "rate-limited"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_dt(value: Any) -> Optional[datetime]:
    """Coerce a datetime / ISO string / epoch float to an aware UTC datetime, else None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    try:
        text = str(value).strip()
        dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _field(window: Any, name: str, default: Any = None) -> Any:
    if isinstance(window, dict):
        return window.get(name, default)
    return getattr(window, name, default)


def window_status(window: Any) -> Optional[str]:
    """Normalized provider status marker for a window, or None when absent."""
    raw = _field(window, "status")
    text = str(raw).strip() if raw is not None else ""
    return text or None


def window_spent(window: Any, now: datetime) -> bool:
    """A window is spent iff its reset is still in the future AND its status/percent says so.

    Precedence (D25): an expired window is NOT spent (rolled forward to 0%, D9); otherwise the
    provider ``status`` marker wins over ``used_percent``; missing status falls back to
    ``used_percent >= 100``.
    """
    reset = as_dt(_field(window, "reset_at"))
    if reset is not None and reset <= now:
        return False
    status = window_status(window)
    if status is not None:
        return status.lower() == SPENT_STATUS
    used = _field(window, "used_percent")
    return (used or 0) >= SPENT_ABOVE_PERCENT


def account_state(
    windows: Iterable[Any],
    *,
    metered: bool,
    metered_exhausted_until: Any = None,
    now: Optional[datetime] = None,
) -> str:
    """``covered`` | ``overflow`` | ``exhausted`` for one account's windows (D1/D9/D47).

    A recorded cushion refusal in the future makes the account ``exhausted`` regardless of the
    stored plan windows: the refusal is the freshest, strongest signal (D47). Enforcement is
    conjunctive - ONE spent window makes the account non-covered.
    """
    now = now or _utc_now()
    refusal = as_dt(metered_exhausted_until)
    if refusal is not None and refusal > now:
        return "exhausted"
    if not [w for w in windows if window_spent(w, now)]:
        return "covered"
    return "overflow" if metered else "exhausted"


def effective_recovery(
    windows: Iterable[Any],
    metered_exhausted_until: Any = None,
    now: Optional[datetime] = None,
) -> datetime:
    """When this account can serve under plan again: LATEST of the binding constraints (D47).

    ``max(plan recovery, metered_exhausted_until)``; ``now`` when nothing binds.
    """
    now = now or _utc_now()
    resets = [
        reset
        for w in windows
        if window_spent(w, now) and (reset := as_dt(_field(w, "reset_at"))) is not None
    ]
    refusal = as_dt(metered_exhausted_until)
    if refusal is not None:
        resets.append(refusal)
    return max(resets) if resets else now


def drain_key(windows: Iterable[Any], now: Optional[datetime] = None) -> Optional[datetime]:
    """Which covered account to spend first: the most-constrained window's reset (D39).

    The window with the highest ``used_percent`` wins; its ``reset_at`` is the key. Only
    meaningful for a covered account (the caller orders by priority first).
    """
    windows = list(windows)
    if not windows:
        return None
    best = min(
        windows,
        key=lambda w: (-(float(_field(w, "used_percent") or 0.0)),
                       as_dt(_field(w, "reset_at")) or datetime.max.replace(tzinfo=timezone.utc)),
    )
    return as_dt(_field(best, "reset_at"))
