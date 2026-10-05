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
    groups = [g for g in quota_state.status_groups() if g["provider"] in targets]
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
        if group.get("hint"):
            print(f"  hint: {group['hint']}")
    for provider, outcome in outcomes.items():
        print(f"refresh {provider}: {outcome}", file=sys.stderr)
    return 0


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
