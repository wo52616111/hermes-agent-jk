"""Quota-aware per-child routing for delegate_task (``delegation.provider: auto``).

An external router command picks provider/model (plus a fallback chain) for each child; the child is then pinned
through the SAME credential path a static ``delegation.provider``/``delegation.model`` uses. On router trouble the
child inherits the parent exactly as if ``provider`` were unset — unless the parent runs on a blocked provider, in
which case the delegation fails (enforced by the caller via ``is_blocked_provider``). Outcomes are reported back to the router fire-and-forget so it can learn which lanes actually delivered.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tools.delegate_tool")  # log-record parity with the origin module

AUTO_PROVIDER = "auto"
ROUTER_ROLES = ("code-core", "code-routine", "screen", "ideate", "review")
DEFAULT_ROUTER_ROLE = "code-core"
DEFAULT_ROUTER_COMMAND = "python3 ~/mygit/eva-mind/clients/claude-code/eva-model.py route --json"
ROUTE_TIMEOUT_SECONDS = 15
RECORD_TIMEOUT_SECONDS = 5
# Hard rule: this lane must never be used as a child fallback, whatever the router says.
_BLOCKED_PROVIDERS = frozenset({"aftership-codex-proxy"})


def is_auto(cfg: Any) -> bool:
    return isinstance(cfg, dict) and str(cfg.get("provider") or "").strip().lower() == AUTO_PROVIDER


def without_auto(cfg: dict) -> dict:
    """The config as the non-auto path should see it (``provider: auto`` removed → inherit the parent)."""
    return {k: v for k, v in cfg.items() if k != "provider"}


def normalize_router_role(raw: Any) -> str:
    role = str(raw or "").strip().lower()
    return role if role in ROUTER_ROLES else DEFAULT_ROUTER_ROLE


def _router_argv(cfg: dict) -> List[str]:
    raw = cfg.get("router_command") or DEFAULT_ROUTER_COMMAND
    argv = [str(a) for a in raw] if isinstance(raw, list) else shlex.split(str(raw))
    return [os.path.expanduser(a) if a.startswith("~") else a for a in argv]


def record_argv(cfg: dict) -> Optional[List[str]]:
    """Router argv with ``route`` → ``record`` and ``--json`` dropped; None when the command has no ``route`` verb."""
    argv = _router_argv(cfg)
    if "route" not in argv:
        return None
    idx = argv.index("route")
    out = argv[:idx] + ["record"] + argv[idx + 1:]
    return [a for a in out if a != "--json"]


def _subprocess_env() -> Optional[dict]:
    try:
        from tools.environments.local import build_subprocess_env
        return build_subprocess_env(scrub_secrets=False, inherit_profile_home=False)
    except Exception:
        return None


def is_blocked_provider(provider: Any) -> bool:
    return str(provider or "").strip().lower() in _BLOCKED_PROVIDERS


def inherited_routing_cfg(base_cfg: dict, parent_agent: Any, creds: Dict[str, Any]) -> dict:
    """``base_cfg`` for a child that could not be routed, with blocked providers removed from the
    fallback chain it would inherit (declared ``fallback_providers`` or the parent's chain)."""
    from tools.delegate_tool_config import _resolve_child_fallback_chain
    pinned = bool(creds.get("provider") or creds.get("base_url") or creds.get("model"))
    chain = _resolve_child_fallback_chain(parent_agent, base_cfg, pinned=pinned)
    if not chain or not any(is_blocked_provider(e.get("provider")) for e in chain if isinstance(e, dict)):
        return base_cfg
    kept = [e for e in chain if isinstance(e, dict) and not is_blocked_provider(e.get("provider"))]
    return {**base_cfg, "fallback_providers": kept}


def call_router(cfg: dict, role: str, author_vendor: Optional[str], task_id: str) -> Optional[Dict[str, Any]]:
    """Run the router for one child; the validated decision, or None (warning logged) on any failure."""
    argv = _router_argv(cfg) + [role]
    if author_vendor:
        argv += ["--author-vendor", str(author_vendor)]
    argv += ["--task-id", task_id]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=ROUTE_TIMEOUT_SECONDS, shell=False, env=_subprocess_env(),
        )
    except Exception as exc:  # timeout, missing binary, ...
        logger.warning("delegation router failed (%s); child inherits the parent model", exc)
        return None
    if proc.returncode != 0:
        logger.warning("delegation router exited %s (%s); child inherits the parent model",
                       proc.returncode, (proc.stderr or "").strip()[:300])
        return None
    try:
        data = json.loads(proc.stdout or "")
    except (TypeError, ValueError) as exc:
        logger.warning("delegation router returned invalid JSON (%s); child inherits the parent model", exc)
        return None
    if not isinstance(data, dict):
        logger.warning("delegation router returned a non-object; child inherits the parent model")
        return None
    provider = str(data.get("provider") or "").strip()
    model = str(data.get("model") or "").strip()
    if not provider or not model:
        logger.warning("delegation router gave no provider/model (reason=%r); child inherits the parent model",
                       data.get("reason"))
        return None
    if is_blocked_provider(provider):
        logger.warning("delegation router chose blocked provider %r; child inherits the parent model", provider)
        return None
    fallback: List[Dict[str, Any]] = []
    for entry in data.get("fallback") or []:
        if not isinstance(entry, dict):
            continue
        fb_provider = str(entry.get("provider") or "").strip()
        fb_model = str(entry.get("model") or "").strip()
        if not fb_provider or not fb_model or is_blocked_provider(fb_provider):
            continue
        fallback.append({"provider": fb_provider, "model": fb_model})
    decision_id = data.get("decision_id")
    return {
        "decision_id": str(decision_id) if decision_id not in (None, "") else None,
        "family": data.get("family"), "provider": provider, "model": model,
        "fallback": fallback, "reason": data.get("reason"),
    }


def routed_child_route(cfg: dict, task: dict, task_id: str) -> Optional[Dict[str, Any]]:
    """Router decision → ``{creds, routing_cfg, routed}`` for one child, or None to inherit the parent.

    ``creds`` comes from the existing pinned-provider resolver; ``routing_cfg`` carries exactly the router's
    fallback chain (``[]`` disables fallback) so ``delegation.fallback_providers`` is ignored for routed children.
    """
    role = normalize_router_role(task.get("route_role"))
    decision = call_router(cfg, role, task.get("author_vendor"), task_id)
    if decision is None:
        return None
    from tools.delegate_tool_config import _runtime_provider_credentials
    values = {"model": decision["model"], "provider": decision["provider"], "base_url": None, "api_key": None,
              "api_mode": str(cfg.get("api_mode") or "").strip().lower() or None}
    explicit = cfg.get("request_overrides") if isinstance(cfg.get("request_overrides"), dict) else None
    try:
        creds = _runtime_provider_credentials(values, explicit)
    except Exception as exc:
        logger.warning("delegation router provider %r is not resolvable (%s); child inherits the parent model",
                       decision["provider"], exc)
        return None
    creds["model"] = decision["model"]
    return {
        "creds": creds,
        "routing_cfg": {"fallback_providers": list(decision["fallback"])},
        "routed": {k: decision[k] for k in ("decision_id", "provider", "model", "reason")},
    }


def _str_attr(obj: Any, name: str) -> Optional[str]:
    value = getattr(obj, name, None)
    return value if isinstance(value, str) and value else None


def finish_routed_child(child: Any, entry: Any) -> None:
    """Stamp ``routed`` onto the result entry and report the outcome to the router (fire-and-forget).

    Never raises and never blocks: the record subprocess runs on a daemon thread with its own timeout.
    """
    try:
        meta = getattr(child, "_delegate_routed", None)
        cfg = getattr(child, "_delegate_router_cfg", None)
        if not isinstance(meta, dict) or not isinstance(entry, dict):
            return
        entry["routed"] = dict(meta)
        decision_id = meta.get("decision_id")
        argv = record_argv(cfg) if isinstance(cfg, dict) else None
        if not decision_id or argv is None:
            return
        outcome = "done" if entry.get("status") == "completed" else "failed"
        argv = argv + [
            str(decision_id), outcome, f"session={_str_attr(child, 'session_id') or ''}",
            f"provider={_str_attr(child, 'provider') or meta.get('provider') or ''}",
            f"model={_str_attr(child, 'model') or meta.get('model') or ''}",
        ]
        _spawn_record(argv)
    except Exception as exc:
        logger.debug("delegation routed-outcome bookkeeping failed: %s", exc)


def _run_record(argv: List[str]) -> None:
    try:
        subprocess.run(argv, capture_output=True, text=True, timeout=RECORD_TIMEOUT_SECONDS, shell=False,
                       env=_subprocess_env())
    except Exception as exc:
        logger.debug("delegation router record failed: %s", exc)


def _spawn_record(argv: List[str]) -> None:
    try:
        from agent.memory_provider import spawn_context_thread
        thread = spawn_context_thread(_run_record, name="delegate-router-record", args=(argv,))
    except Exception:
        import threading
        thread = threading.Thread(target=_run_record, args=(argv,), name="delegate-router-record", daemon=True)
    thread.start()
