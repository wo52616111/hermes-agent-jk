"""Locked credential-pool administration and target resolution."""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from agent.credential_pool import PooledCredential


def target_names_identity(
    target: Any, entry_id: Any, label: Any, source: Any, index: Optional[int] = None,
) -> bool:
    """Whether *target* names the account carrying these identity fields (D45).

    The ONE implementation of the accepted target FORMS: exact entry id, label (case-
    insensitive), ``env:<VAR>`` source, and a bare ``<VAR>`` normalised to ``env:<VAR>``
    (the form a user sees in ``credential_pool_policy``). ``index`` adds the 1-based
    position form. Precedence between forms is applied by :func:`resolve_entry_target`;
    this predicate answers only "does this target name THIS account", so callers that
    classify rather than resolve (the metered-account classification) share the same
    forms instead of a second, divergent matcher.
    """
    raw = str(target or "").strip()
    if not raw:
        return False
    if entry_id is not None and raw == str(entry_id):
        return True
    if label is not None and str(label).strip() and raw.lower() == str(label).strip().lower():
        return True
    src = str(source or "").strip()
    if src:
        env_ref = raw if raw.lower().startswith("env:") else f"env:{raw}"
        if src.lower() == env_ref.lower():
            return True
    if index is not None and raw.isdigit() and int(raw) == index:
        return True
    return False


def resolve_entry_target(
    target: Any, entries: List["PooledCredential"],
) -> Tuple[Optional[int], Optional["PooledCredential"], Optional[str]]:
    """Resolve a target to a pool entry by the D45 precedence (the ONE owner).

    Order: exact entry id > unique exact label > ``env:<VAR>`` source > bare ``<VAR>``
    normalised to ``env:<VAR>`` > 1-based index. Returns ``(1-based index, entry, None)``
    or ``(None, None, reason)``; an ambiguous label/source is unresolvable, never a
    silent first match.
    """
    raw = str(target or "").strip()
    if not raw:
        return None, None, "No credential target provided."

    for idx, entry in enumerate(entries, start=1):
        if str(getattr(entry, "id", "") or "") == raw:
            return idx, entry, None

    label_hits = [
        (idx, entry)
        for idx, entry in enumerate(entries, start=1)
        if str(getattr(entry, "label", "") or "").strip()
        and raw.lower() == str(entry.label).strip().lower()
    ]
    if len(label_hits) == 1:
        return label_hits[0][0], label_hits[0][1], None
    if len(label_hits) > 1:
        return None, None, f'Ambiguous credential label "{raw}". Use the numeric index or entry id instead.'

    env_ref = raw if raw.lower().startswith("env:") else f"env:{raw}"
    env_hits = [
        (idx, entry)
        for idx, entry in enumerate(entries, start=1)
        if str(getattr(entry, "source", "") or "").strip()
        and env_ref.lower() == str(entry.source).strip().lower()
    ]
    if len(env_hits) == 1:
        return env_hits[0][0], env_hits[0][1], None
    if len(env_hits) > 1:
        return None, None, f'Ambiguous credential source "{env_ref}". Use the numeric index or entry id instead.'

    if raw.isdigit():
        index = int(raw)
        if 1 <= index <= len(entries):
            return index, entries[index - 1], None
        return None, None, f"No credential #{index}."
    return None, None, f'No credential matching "{raw}".'


def _cleared_status_copy(entry: PooledCredential) -> PooledCredential:
    from agent.credential_pool import _CLEAR_STATUS

    # The reset marker lets a live pool in another process tell "reset after my cooldown" from
    # "never had a status" — both read as bare None on disk (#89415).
    return replace(entry, **_CLEAR_STATUS, model_cooldowns=None, status_cleared_at=time.time(),
                   extra={k: v for k, v in entry.extra.items() if k != "failure_reason"})


class CredentialPoolAdminMixin:
    def reset_status(self, credential_id: str) -> Optional[PooledCredential]:
        """Clear only the target's local error state, preserving sibling cooldowns."""
        with self._lock:
            entry = self._find(lambda e: e.id == credential_id)
            if entry is None:
                return None
            cleared = _cleared_status_copy(entry)
            self._replace_entry(entry, cleared)
            self._persist(status_cleared_ids=[cleared.id])
            return cleared
    def reset_statuses(self) -> int:
        """Clear exhaustion state on every entry. Returns how many were cleared.

        ``failure_reason`` lives in ``extra``, not a dataclass field, so it is
        stripped explicitly. The persist declares the cleared ids because the
        disk-recency merge reads a cleared ``last_status_at`` (None -> epoch 0)
        as a stale snapshot and would copy a still-binding cooldown back.
        """
        from agent.credential_pool import _CLEAR_STATUS

        with self._lock:
            stale = [
                e for e in self._entries
                if e.last_status or e.last_status_at or e.last_error_code or e.failure_reason or e.model_cooldowns
            ]
            if stale:
                stale_ids = {e.id for e in stale}
                self._entries = [
                    _cleared_status_copy(e) if e.id in stale_ids else e
                    for e in self._entries
                ]
                self._persist(status_cleared_ids=list(stale_ids))
            return len(stale)

    def remove_index(self, index: int) -> Optional[PooledCredential]:
        from agent.credential_pool import persist_pool_entries

        with self._lock:
            if index < 1 or index > len(self._entries):
                return None
            removed = self._entries.pop(index - 1)
            self._entries = [replace(e, priority=p) for p, e in enumerate(self._entries)]
            persist_pool_entries(
                self.provider,
                [entry.to_dict() for entry in self._entries],
                removed_ids=[removed.id],
            )
            if self._current_id == removed.id:
                self._current_id = None
            return removed

    def move_entry(self, credential_id: str, priority: int) -> Optional[PooledCredential]:
        """Place an entry at a clamped zero-based position and persist contiguous priorities."""
        from agent.credential_pool import _normalize_pool_priorities

        with self._lock:
            entry = self._find(lambda e: e.id == credential_id)
            if entry is None:
                return None
            others = [e for e in self._entries if e.id != credential_id]
            others.insert(max(0, min(int(priority), len(others))), entry)
            entries = [replace(e, priority=p) for p, e in enumerate(others)]
            # Apply load-time ordering now so the reported position survives reload.
            _normalize_pool_priorities(self.provider, entries)
            self._entries = sorted(entries, key=lambda e: e.priority)
            self._persist()
            return self._find(lambda e: e.id == credential_id)

    def resolve_target(self, target: Any) -> Tuple[Optional[int], Optional[PooledCredential], Optional[str]]:
        with self._lock:
            return resolve_entry_target(target, self._entries)

    def add_entry(self, entry: PooledCredential) -> PooledCredential:
        from agent.credential_pool import _next_priority, write_credential_pool

        with self._lock:
            entry = replace(entry, priority=_next_priority(self._entries))
            self._entries.append(entry)
            borrowed_ids = getattr(self, "_borrowed_root_ids", None)
            if borrowed_ids:
                # ``hermes -p <profile> auth add <single-use provider>``: the
                # profile claims its OWN credential. Persist only profile-owned
                # rows — copying the borrowed root grant alongside would fork
                # its single-use refresh token (#100339). Once the profile owns
                # rows, the root fallback for this provider is shadowed.
                self._entries = [e for e in self._entries if e.id not in borrowed_ids]
                write_credential_pool(self.provider, [e.to_dict() for e in self._entries])
                self._borrowed_root_ids = set()
            else:
                self._persist()
            return entry
