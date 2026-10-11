"""Pure quota-aware ranking and stickiness (spec D3/D4/D10/D12/D21/D39/D43/D47).

This module performs NO IO: callers pass the pool entries, a per-entry state map and a
resolved policy; the module only sorts and picks. The pool bridges stored facts to states
(:func:`states_from_view`, fed by the ONE profile-scoped reader ``quota_state.read_view``)
and supplies the policy; both are read-only, so every selection checkpoint is a pure
decision over stored, read-time-rolled state (D11).

Total order (D10/D21/D39/D47): ``covered`` < ``unknown`` < ``overflow`` < ``exhausted``;
inside ``covered`` the key is ``(priority, drain_key)`` when ``earliest_reset_first`` is set
(priority wins, D39) else ``(priority)``; inside ``exhausted`` it is
``(effective_recovery, priority)``; ``unknown``/``overflow`` order by priority. The class
term is never crossed by a tie-break, so a covered account always beats an overflow one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from agent import quota_aware_state as st

COVERED = "covered"
UNKNOWN = "unknown"
OVERFLOW = "overflow"
EXHAUSTED = "exhausted"

# The class ordering (D10). A smaller value sorts first.
CLASS = {COVERED: 0, UNKNOWN: 1, OVERFLOW: 2, EXHAUSTED: 3}

# Assignment modes (D4/D43): a fresh session/child may spread across covered accounts;
# a live session only moves on a real signal or at a turn boundary.
MODE_SESSION_START = "session_start"
MODE_TURN_BOUNDARY = "turn_boundary"

# D39 (matches D5's config example): ``earliest_reset_first`` is TRUE when the policy block
# omits it; treating a missing flag as False would silently disable the same-priority drain
# tie-break the user asked for.
EARLIEST_RESET_FIRST_DEFAULT = True


@dataclass(frozen=True)
class SelectionState:
    """The read-time state of one account, as consumed by the ordering (D9/D47)."""

    state: str = UNKNOWN
    windows: tuple = ()
    effective_recovery: Optional[datetime] = None


# A missing/unknown account: sorts after every known-covered one but is never erased.
UNKNOWN_STATE = SelectionState()


@dataclass(frozen=True)
class Policy:
    """The ordering-shaped half of ``credential_pool_policy`` (D5/D39)."""

    earliest_reset_first: bool = EARLIEST_RESET_FIRST_DEFAULT


def _id_of(entry: Any) -> Optional[str]:
    return getattr(entry, "id", None)


def _priority_of(entry: Any) -> int:
    try:
        return int(getattr(entry, "priority", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _max_dt() -> datetime:
    return datetime.max.replace(tzinfo=timezone.utc)


def _state_of(states: dict, entry: Any) -> SelectionState:
    return states.get(_id_of(entry), UNKNOWN_STATE)


def sort_key(entry: Any, state: SelectionState, policy: Policy, now: datetime):
    """The ordering key for one entry (D21/D39/D47). Class is always the first term."""
    cls = CLASS.get(state.state, CLASS[UNKNOWN])
    if state.state == COVERED:
        if policy.earliest_reset_first:
            # Priority wins; drain_key only breaks ties WITHIN one priority (D39).
            return (cls, _priority_of(entry), st.drain_key(state.windows, now) or _max_dt())
        return (cls, _priority_of(entry))
    if state.state == EXHAUSTED:
        return (cls, state.effective_recovery or now, _priority_of(entry))
    return (cls, _priority_of(entry))


def ordered_candidates(entries: Iterable[Any], states: dict, policy: Policy, now: datetime) -> list:
    """Every candidate ordered by the total order. Stable for equal keys."""
    return sorted(entries, key=lambda e: sort_key(e, _state_of(states, e), policy, now))


def _by_id(entries: list, credential_id: Optional[str]):
    if credential_id is None:
        return None
    return next((e for e in entries if _id_of(e) == credential_id), None)


def _first_covered(ordered: list, states: dict):
    return next((e for e in ordered if _state_of(states, e).state == COVERED), None)


def choose(
    entries: Iterable[Any],
    states: dict,
    policy: Policy,
    current_id: Optional[str],
    mode: str,
    now: datetime,
):
    """Pick the account to serve, honouring stickiness (D4/D22/D43).

    ``session_start`` (or no current binding) takes the head of the total order, so a new
    session/child may spread across covered accounts. A live session keeps its account while
    it is ``covered`` or ``unknown`` (fail-open sticky); it moves to a covered sibling when
    the current account is ``overflow``, and re-orders when the current account is
    ``exhausted``. A local concurrency counter is never consulted here (D43).
    """
    entries = list(entries)
    if not entries:
        return None
    ordered = ordered_candidates(entries, states, policy, now)
    current = _by_id(entries, current_id)
    if current is None or mode == MODE_SESSION_START:
        return ordered[0]
    cur_state = _state_of(states, current)
    if cur_state.state in (COVERED, UNKNOWN):
        return current
    if cur_state.state == OVERFLOW and any(
        _state_of(states, e).state == COVERED for e in entries
    ):
        return _first_covered(ordered, states) or current
    if cur_state.state == EXHAUSTED:
        return ordered[0]
    return current


def states_from_view(entries: Iterable[Any], view: Optional[dict], now: datetime) -> dict:
    """Map pool entries to :class:`SelectionState` using a ``read_view`` payload (pure).

    The account is keyed by credential fingerprint (D13): a pool entry carries its
    ``secret_fingerprint`` in ``extra``. An entry with no matching account is ``unknown``
    (no usable evidence) and never outranks a known-covered one (D16).
    """
    by_fp: dict = {}
    for account in (view or {}).get("accounts") or []:
        fingerprint = account.get("fingerprint")
        if fingerprint:
            by_fp[fingerprint] = account
    out: dict = {}
    for entry in entries:
        fingerprint = (getattr(entry, "extra", None) or {}).get("secret_fingerprint")
        record = by_fp.get(fingerprint)
        if record is None:
            out[_id_of(entry)] = SelectionState(UNKNOWN, (), None)
            continue
        out[_id_of(entry)] = SelectionState(
            state=str(record.get("state") or UNKNOWN),
            windows=tuple(record.get("windows") or ()),
            effective_recovery=st.as_dt(record.get("recovery_at")) or now,
        )
    return out
