"""Subscription-quota wire groups, built once for every surface that shows them.

The status bar reads ``account_usage*`` off the usage payload. Two transports produce that
payload — the legacy TUI backend (``tui_gateway/server.py``) and the canonical session
authority (``gateway/session_usage.py``) — and a second copy of these builders is how one
transport silently loses a provider's windows. This module is the single source; callers
differ only in which reader they hand over.
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def window_wire(window: dict) -> dict:
    """One quota window as the wire carries it (the per-account view's own window shape)."""
    return {k: window.get(k) for k in
            ("period", "used_percent", "reset_at", "rolled", "status", "window_seconds")}


def account_wire(provider: str, account: dict) -> dict:
    """One credential of a ``quota_aware`` provider's view (D19/D26). ``error_status`` is read from
    the account's stored FACTS (the view keeps only the message) so the UI can mark a 429 read
    without parsing text."""
    from agent import quota_state
    out = {
        "fingerprint": account.get("fingerprint"),
        "label": account.get("label"),
        "entry_id": account.get("entry_id"),
        "state": account.get("state"),
        "metered": bool(account.get("metered")),
        "read_health": account.get("read_health"),
        "windows": [window_wire(w) for w in account.get("windows") or []],
        "fetched_at": account.get("fetched_at"),
        "age_s": account.get("age_s"),
        "attempted_at": account.get("attempted_at"),
        "last_error": account.get("last_error"),
        "error_status": None,
        "backoff_until": account.get("backoff_until"),
        "recovery_at": account.get("recovery_at"),
    }
    try:
        fingerprint = account.get("fingerprint")
        if isinstance(fingerprint, str) and fingerprint:
            facts = quota_state.read_account_facts(provider, fingerprint) or {}
            out["error_status"] = (facts.get("error") or {}).get("status")
    except Exception:
        logger.debug("account facts read failed for %s", provider, exc_info=True)
    return out


def group_from_view(provider: str, view: dict) -> dict:
    """A capacity-row group built from a ``quota_aware`` provider's per-profile view (D41).

    The provider line carries the SERVING account's own windows - never a sum or an average
    (D14/D34) - plus every account's own row, so a multi-account pool stops reporting empty
    windows. ``serving`` is the shared fact; the session-local active account is added by
    :func:`fields`.
    """
    from agent import quota_state
    accounts = list(view.get("accounts") or [])
    serving = quota_state.serving_account(view)
    return {
        "provider": provider,
        "fetched_at": serving.get("fetched_at"),
        "age_s": serving.get("age_s"),
        "error": None,
        "backoff_until": serving.get("backoff_until"),
        "windows": [window_wire(w) for w in serving.get("windows") or []],
        "hint": None,
        "multi_account": len(accounts) > 1,
        "serving": view.get("serving"),
        "serving_since": view.get("serving_since"),
        "states": dict(view.get("states") or {}),
        "strategy": view.get("strategy"),
        "accounts": [account_wire(provider, a) for a in accounts],
    }


def view_groups() -> list[dict]:
    """Per-account groups for every provider whose profile-scoped view exists (D41)."""
    from agent import quota_state
    profile = quota_state.current_profile()
    groups = []
    for provider in quota_state.SUPPORTED:
        try:
            view = quota_state.read_view(profile, provider)
        except Exception:
            logger.debug("quota view read failed for %s", provider, exc_info=True)
            continue
        if view and view.get("accounts"):
            groups.append(group_from_view(provider, view))
    return groups


def all_groups() -> list[dict]:
    """Every provider with stored quota, for the capacity row (status bar).

    Legacy providers come from ``quota_state.status_groups()`` unchanged; a provider with a
    ``quota_aware`` per-profile view (D41) is replaced by its per-account group, which carries
    every account's own windows.
    """
    try:
        from agent import quota_state
        groups = quota_state.status_groups()
    except Exception:
        logger.debug("quota status read failed", exc_info=True)
        groups = []
    try:
        view_by_provider = view_groups()
    except Exception:
        logger.debug("quota view groups read failed", exc_info=True)
        return groups
    if not view_by_provider:
        return groups
    by_provider = {g["provider"]: g for g in view_by_provider}
    merged = [by_provider.pop(g["provider"], g) for g in groups]
    merged.extend(by_provider.values())
    return merged


def legacy_account_usage(provider: str, groups: list[dict]) -> dict | None:
    """The single-provider ``account_usage`` older clients still render."""
    for group in groups:
        if group.get("provider") == provider and group.get("windows"):
            return {"provider": provider, "fetched_at": group.get("fetched_at"),
                    "windows": [{k: w.get(k) for k in ("period", "used_percent", "reset_at")}
                                for w in group["windows"]]}
    return None


def active_fingerprint(provider: str, api_key: str | None = None) -> str | None:
    """Fingerprint of the credential THIS session is on (D19/D29). Resolved from the live agent's
    own key so two sessions on different accounts do not share one "active" flag; None when
    unresolved (the TUI then falls back to the group's ``serving``)."""
    if not provider:
        return None
    try:
        from agent import quota_state
        return quota_state.served_fingerprint(provider, api_key=api_key)
    except Exception:
        logger.debug("active account resolution failed for %s", provider, exc_info=True)
        return None


def age_groups(groups: list[dict], now: datetime | None = None) -> list[dict]:
    """Recompute snapshot ages from stored fetch times without touching credentials or providers.

    An age frozen at write time makes a stale snapshot look fresh for as long as the reader
    keeps the payload; every reader recomputes it instead.
    """
    now = now or datetime.now().astimezone()
    aged = []
    for group in groups:
        next_group = _reaged(group, now)
        accounts = group.get("accounts")
        if isinstance(accounts, list):
            # Each account ages from ITS OWN ``fetched_at``: the idle clock must not leave an
            # account's ``age_s`` frozen at the value the view was read with, or a stale account
            # keeps looking fresh while the provider line's age advances.
            next_group["accounts"] = [
                _reaged(account, now) if isinstance(account, dict) else account for account in accounts
            ]
        aged.append(next_group)
    return aged


def _reaged(record: dict, now: datetime) -> dict:
    """A copy of ``record`` with ``age_s`` recomputed from its own ``fetched_at`` (left untouched
    when the timestamp is missing or unparseable)."""
    next_record = dict(record)
    try:
        fetched = datetime.fromisoformat(str(record.get("fetched_at") or ""))
        if fetched.tzinfo is None:
            fetched = fetched.astimezone()
        next_record["age_s"] = max(0, int((now - fetched).total_seconds()))
    except (TypeError, ValueError):
        pass
    return next_record


def wire_provider(agent) -> str:
    """The provider name the capacity row keys on: opencode-go's Zen pool has its own group."""
    provider = str(getattr(agent, "provider", "") or "").strip().lower()
    base_url = str(getattr(agent, "base_url", "") or "").lower()
    return "opencode-go" if "opencode.ai" in base_url and "/zen/go" in base_url else provider


def fields(agent) -> dict:
    """The four ``account_usage*`` keys for one session's agent, aged at read time.

    Absent figures stay absent (``None``), never fabricated as zeros: a provider with no stored
    quota must not render a 0% bar.
    """
    groups = age_groups(all_groups())
    provider = wire_provider(agent)
    return {
        "account_usage_all": groups,
        "account_usage": legacy_account_usage(provider, groups),
        "account_usage_active": provider or None,
        "account_usage_active_account": active_fingerprint(
            provider, api_key=getattr(agent, "api_key", None)),
    }
