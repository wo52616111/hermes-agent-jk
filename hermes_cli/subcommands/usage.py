"""``hermes usage`` — the account-limits block of the REPL ``/usage`` without starting a session.

Script-friendly Codex / Anthropic / OpenRouter quota view (issue #33094): same fetch and renderer as
``/usage`` (``agent.account_usage``), same credential resolution as a session with no live agent, plus
``--json`` for cron jobs and shell loops. Slim redo of #81819 (@himanusia).
"""

from __future__ import annotations

import argparse
import json
import sys


def usage_snapshot_document(snapshot) -> dict:
    """``hermes usage --json`` document. Schema is documented in website/docs/reference/cli-commands.md —
    keep the keys stable; extend only by adding keys."""
    return {
        "provider": snapshot.provider,
        "source": snapshot.source,
        "title": snapshot.title,
        "plan": snapshot.plan,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "windows": [
            {
                "label": window.label,
                "used_percent": window.used_percent,
                "resets_at": window.reset_at.isoformat() if window.reset_at else None,
                "detail": window.detail,
            }
            for window in snapshot.windows
        ],
        "details": list(snapshot.details),
        "unavailable_reason": snapshot.unavailable_reason,
    }


def _short_fingerprint(fingerprint) -> str | None:
    """``sha256:6e4e98…`` -> ``6e4e98``: enough to tell two accounts apart, never key material."""
    raw = str(fingerprint or "")
    if not raw:
        return None
    digest = raw.split(":", 1)[1] if ":" in raw else raw
    return digest[:6]


def _usage_view(provider: str) -> dict | None:
    """The profile-scoped view for ``provider`` (D41), or None for a provider without one."""
    from agent import quota_state
    try:
        return quota_state.read_view(quota_state.current_profile(), provider)
    except Exception:  # noqa: BLE001 — the CLI must never crash on a bad view
        return None


def _account_record(account: dict) -> dict:
    """One account as the CLI's nested document carries it: short fingerprint, effective ``state``
    and ``read_health``, this account's own windows. Never key material."""
    return {
        "label": account.get("label"),
        "fingerprint": _short_fingerprint(account.get("fingerprint")),
        "state": account.get("state"),
        "metered": bool(account.get("metered")),
        "read_health": account.get("read_health"),
        "last_error": account.get("last_error"),
        "attempted_at": account.get("attempted_at"),
        "age_s": account.get("age_s"),
        "windows": [
            {"period": w.get("period"), "used_percent": w.get("used_percent"),
             "resets_at": w.get("reset_at"), "status": w.get("status")}
            for w in account.get("windows") or []
        ],
    }


def _usage_accounts_document(provider: str) -> list[dict]:
    """Nested per-account records (D20): effective ``state`` AND ``read_health`` per account, read
    through ``read_view``. Empty for a provider whose legacy group is unchanged."""
    view = _usage_view(provider)
    if not view:
        return []
    return [_account_record(account) for account in view.get("accounts") or []]


def _subscription_group_from_view(provider: str, view: dict) -> dict:
    """A subscription group built from a ``quota_aware`` provider's per-profile view (D41).

    A ``quota_aware`` provider no longer writes a legacy ``status_groups()`` snapshot (B1), so a
    subscription that exists only as per-account facts has no group to attach to: build one here so
    ``hermes usage --all-subscriptions`` never reports an empty list for a provider the gateway
    renders. ``windows`` is the SERVING account's own - never a sum or an average (D14/D34).
    """
    accounts = list(view.get("accounts") or [])
    by_fp = {a.get("fingerprint"): a for a in accounts}
    serving = by_fp.get(view.get("serving")) or (accounts[0] if accounts else {})
    return {
        "provider": provider,
        "fetched_at": serving.get("fetched_at"),
        "age_s": serving.get("age_s"),
        "error": None,
        "backoff_until": serving.get("backoff_until"),
        "windows": [
            {"period": w.get("period"), "used_percent": w.get("used_percent"), "reset_at": w.get("reset_at")}
            for w in serving.get("windows") or []
        ],
        "hint": None,
        "multi_account": len(accounts) > 1,
        "states": dict(view.get("states") or {}),
        "strategy": view.get("strategy"),
        "accounts": [_account_record(a) for a in accounts],
    }


def _refresh_accounts(provider: str) -> int:
    """Synchronously re-fetch every account of a per-account provider (D20/D42): the user asked with
    ``--refresh``. Returns how many accounts were fetched."""
    from agent import quota_state
    view = _usage_view(provider)
    if not view:
        return 0
    fetched = 0
    for account in view.get("accounts") or []:
        fingerprint = account.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            continue
        try:
            quota_state.fetch_account_facts(provider, fingerprint)
            fetched += 1
        except Exception:  # noqa: BLE001 — one account's failure must not abort the rest
            continue
    return fetched


def cmd_usage(args: argparse.Namespace) -> int:
    """Print the configured (or ``--provider``) account's usage windows; exit 1 when nothing could be fetched."""
    if getattr(args, "subscriptions", False) or getattr(args, "refresh", False):
        return _cmd_usage_subscriptions(args)
    from agent.account_usage import fetch_account_usage, render_account_usage_lines
    from hermes_cli.runtime_provider import resolve_requested_provider

    provider = resolve_requested_provider(getattr(args, "provider", None))
    # No explicit key: the fetcher resolves the credential exactly as a session without a live agent
    # would (singleton store, then credential pool) — it never adopts or refreshes anything else.
    snapshot = fetch_account_usage(provider)
    if snapshot is None:
        print(
            f"No account usage available for provider '{provider}': no credential is configured for it, "
            "the provider has no usage endpoint, or the fetch failed.",
            file=sys.stderr,
        )
        return 1
    if getattr(args, "json", False):
        print(json.dumps(usage_snapshot_document(snapshot), indent=2))
    else:
        print("\n".join(render_account_usage_lines(snapshot)))
    return 0


def _cmd_usage_subscriptions(args: argparse.Namespace) -> int:
    """Shared cross-process subscription quota (``agent.quota_state``): every connected subscription,
    read from the on-disk state. ``--refresh`` applies the refresh rule (only billed-since-last-fetch
    or never-fetched providers; ``--stale N`` also re-fetches snapshots older than N seconds), always
    honouring 429 backoff. Used by external routers (eva-model.py) and scripts."""
    from agent import quota_state

    providers = [p.strip().lower() for p in (getattr(args, "providers", None) or []) if p.strip()]
    targets = providers or list(quota_state.SUPPORTED)
    outcomes = {}
    if getattr(args, "refresh", False):
        stale = getattr(args, "stale", None)
        for provider in targets:
            try:
                outcomes[provider] = quota_state.refresh(provider, stale_s=stale)
            except Exception as exc:  # noqa: BLE001 — report, never crash a router call
                outcomes[provider] = f"error: {type(exc).__name__}"
            # A provider with per-account views is refreshed per account (D20/D42): the legacy
            # provider snapshot no longer owns its state.
            accounts = _refresh_accounts(provider)
            if accounts:
                outcomes[provider] = f"{outcomes[provider]}; {accounts} accounts"
    groups = [g for g in quota_state.status_groups() if g["provider"] in targets]
    for group in groups:
        accounts = _usage_accounts_document(group["provider"])
        if accounts:
            group["accounts"] = accounts
    # A provider whose state exists only as per-account facts (B1 stopped writing a legacy
    # snapshot for quota_aware providers) has no status group above: add it from its view so the
    # subscriptions array is never silently missing a provider the gateway renders.
    known = {group["provider"] for group in groups}
    for provider in targets:
        if provider in known:
            continue
        view = _usage_view(provider)
        if view and view.get("accounts"):
            groups.append(_subscription_group_from_view(provider, view))
    if getattr(args, "json", False):
        print(json.dumps({"subscriptions": groups, "refresh": outcomes}, indent=2))
        return 0
    if not groups:
        print("No subscription quota stored yet (run with --refresh).", file=sys.stderr)
        return 1
    for group in groups:
        age = group.get("age_s")
        tail = f"  (fetched {age // 60}m ago)" if age is not None else ""
        if group.get("multi_account"):
            tail = "  multiple accounts in the credential pool: quota unknown"
        elif group.get("error"):
            tail += f"  last fetch failed: {group['error'].get('status')}"
        print(f"{group['provider']}{tail}")
        for window in group["windows"]:
            mark = " (rolled over)" if window.get("rolled") else ""
            print(f"  {window['period']:9} {window['used_percent']:5.1f}%  resets {window.get('reset_at') or '-'}{mark}")
        for account in group.get("accounts") or []:
            _print_account_line(account)
        if group.get("hint"):
            print(f"  hint: {group['hint']}")
    for provider, outcome in outcomes.items():
        print(f"refresh {provider}: {outcome}", file=sys.stderr)
    return 0


def _print_account_line(account: dict) -> None:
    """One per-account sub-row: label + short fingerprint, effective state and read health, and this
    account's own windows. Never key material."""
    label = account.get("label") or account.get("fingerprint") or "-"
    fingerprint = account.get("fingerprint")
    ident = f"{label} [{fingerprint}]" if fingerprint else str(label)
    state = str(account.get("state") or "unknown")
    if state == "overflow" and account.get("metered"):
        state = "overflow(metered)"
    windows = "·".join(
        f"{w.get('period')} {(w.get('used_percent') or 0):.0f}%" for w in account.get("windows") or []
    )
    line = f"  • {ident:34} {state:17} {account.get('read_health') or 'unknown'}"
    if windows:
        line += f"  {windows}"
    print(line)


def build_usage_parser(subparsers) -> None:
    """Attach the ``usage`` subcommand to ``subparsers``."""
    usage_parser = subparsers.add_parser(
        "usage", help="Show account rate-limit windows (the /usage block) without starting a session",
        description="Fetch the configured provider's account limits (Codex 5h/weekly windows, plan, banked "
                    "resets; Anthropic OAuth windows; OpenRouter credits) — the same block the /usage slash "
                    "command prints — and exit. Exit code 1 when no credential is configured or the fetch fails.",
    )
    usage_parser.add_argument(
        "--provider", default=None, help="Provider to query (default: the configured model provider)")
    usage_parser.add_argument(
        "--json", action="store_true", help="Print one JSON document instead of the human-readable block")
    usage_parser.add_argument(
        "--all-subscriptions", dest="subscriptions", action="store_true",
        help="Show every connected subscription (Anthropic / Codex / OpenCode Go) from the shared quota state")
    usage_parser.add_argument(
        "--refresh", action="store_true",
        help="With the shared state: fetch providers billed since their last fetch (or never fetched); "
             "honours 429 backoff. Implies --all-subscriptions")
    usage_parser.add_argument(
        "--stale", type=int, default=None, metavar="SECONDS",
        help="With --refresh: also re-fetch snapshots older than SECONDS")
    usage_parser.add_argument(
        "providers", nargs="*", help="With --all-subscriptions/--refresh: limit to these providers")
    usage_parser.set_defaults(func=cmd_usage)
