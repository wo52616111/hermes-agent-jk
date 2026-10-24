"""Quota-aware delegation router as a ``delegation.backend`` plugin (``delegation.backend: router``).

One external command decides each child's provider/model: ``eva-model.py route <role> --json``. The answer becomes
a normal route (``provider``/``model``/``fallback_providers``), so credential pinning, fallback policy and the
blocked-provider rule all ride the framework paths the ``default`` backend uses.

Rules this backend owns:

* **Blocked providers** (the company proxy and its endpoint) are a hard constant — never a subagent route, and
  never inherited from a parent that runs on one.
* **Router trouble inherits the parent** — an empty route plus the parent's filtered fallback chain, labelled so
  the result says the child was not routed. A router script that cannot be found is a refusal naming the command
  (silent degradation to the parent's model is exactly the failure this backend must not hide).
* **Outcome feedback** — ``task_finished`` is the generic hook core calls once per settled entry; it reports
  ``done``/``failed`` to the router so quota routing learns which lane actually delivered.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
import uuid
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from agent.delegation_provider import DelegationProvider, DelegationRouteError

logger = logging.getLogger("tools.delegate_tool")  # log-record parity with the origin module

BACKEND_NAME = "router"

#: Roles the router understands (``eva-model.py route <role>``).
ROUTER_ROLES = ("code-core", "code-routine", "screen", "ideate", "review")
DEFAULT_ROUTER_ROLE = "code-core"
#: Vendors eva's routing table knows (``eva_routing.FAMILY``): an enum here keeps a value starting with ``--``
#: from ever becoming a flag on the router command line.
AUTHOR_VENDORS = ("anthropic", "openai", "opencode", "open-design")
DEFAULT_ROUTER_COMMAND = "python3 ~/mygit/eva-mind/clients/claude-code/eva-model.py route --json"
DEFAULT_TIMEOUT_SECONDS = 15
RECORD_TIMEOUT_SECONDS = 5

#: Hard rule: these never become a child route, whatever the router says and whatever the parent runs.
BLOCKED_PROVIDERS = frozenset({"aftership-codex-proxy"})
BLOCKED_HOSTS = frozenset({"codex-proxy.aftership.me"})


def _router_argv(raw: Any) -> list[str]:
    argv = [str(a) for a in raw] if isinstance(raw, (list, tuple)) else shlex.split(str(raw or ""))
    return [os.path.expanduser(a) if a.startswith("~") else a for a in argv] or \
        [os.path.expanduser(a) if a.startswith("~") else a for a in shlex.split(DEFAULT_ROUTER_COMMAND)]


def record_argv(argv: list[str]) -> Optional[list[str]]:
    """``route`` → ``record``, ``--json`` dropped; None when the command has no ``route`` verb."""
    if "route" not in argv:
        return None
    idx = argv.index("route")
    return [a for a in argv[:idx] + ["record"] + argv[idx + 1:] if a != "--json"]


def unresolvable_token(argv: list[str]) -> Optional[str]:
    """The first argv token that cannot run: ``shutil.which`` for a bare command, ``exists`` for a path.

    Deliberately not an executable-bit check: ``eva-model.py`` is a script argument of ``python3``, not an
    executable in its own right.
    """
    for i, token in enumerate(argv):
        if not isinstance(token, str) or not token:
            return token or "<empty>"
        if token.startswith("-"):
            continue
        if "/" in token or token.startswith("~") or token.startswith("."):
            if not os.path.exists(os.path.expanduser(token)):
                return token
        elif i == 0 and shutil.which(token) is None:
            return token
    return None


def _host(url: Any) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def is_blocked(*, provider: Any = None, requested_provider: Any = None, base_url: Any = None) -> bool:
    """Three signals, because a named custom provider reports ``provider="custom"`` and keeps its name (or only its
    endpoint) elsewhere."""
    names = {str(provider or "").strip().lower(), str(requested_provider or "").strip().lower()}
    if names & BLOCKED_PROVIDERS:
        return True
    return _host(base_url) in BLOCKED_HOSTS


def _trim_chain(chain: Any) -> list[dict[str, Any]]:
    """Parent fallback entries without their secrets: ``api_key``/``key_env`` never cross the plugin boundary."""
    trimmed: list[dict[str, Any]] = []
    for entry in chain if isinstance(chain, list) else []:
        if not isinstance(entry, Mapping) or not entry.get("provider"):
            continue
        if is_blocked(provider=entry.get("provider"), requested_provider=entry.get("requested_provider"),
                      base_url=entry.get("base_url")):
            continue
        item: dict[str, Any] = {"provider": entry.get("provider")}
        if entry.get("model"):
            item["model"] = entry.get("model")
        if entry.get("base_url"):
            item["base_url"] = entry.get("base_url")
        trimmed.append(item)
    return trimmed


class RouterDelegationProvider(DelegationProvider):
    """``delegation.backend: router`` — the external quota router decides each child."""

    @property
    def name(self) -> str:
        return BACKEND_NAME

    @property
    def display_name(self) -> str:
        return "Router"

    @property
    def summary(self) -> str:
        return "An external router (eva-model.py) picks each subagent's provider/model from live quota."

    def is_available(self) -> bool:
        # No config here, and a bool cannot name the command: the effective-command check lives in
        # ``route_task`` where the config is, so a broken router is a refusal that names it.
        return True

    def settings(self, config: Mapping[str, Any]) -> Mapping[str, Any]:
        """``delegation.backends.router`` — the backend's own block (never beside the core keys)."""
        return dict(super().settings(config))

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            "route_role": {
                "type": "string",
                "enum": list(ROUTER_ROLES),
                "description": ("Optional quota-routing lane for this task (default "
                                f"{DEFAULT_ROUTER_ROLE}); only the 'router' delegation backend reads it."),
            },
            "author_vendor": {
                "type": "string",
                "enum": list(AUTHOR_VENDORS),
                "description": "Optional vendor that authored the work under review (routing hint).",
            },
        }

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        settings = dict(self.settings(config))
        argv = _router_argv(settings.get("router_command"))
        missing = unresolvable_token(argv)
        if missing:
            raise DelegationRouteError(
                f"the 'router' delegation backend cannot run its router command: {missing!r} was not found. "
                f"Fix delegation.backends.router.router_command (currently {' '.join(argv)}) or run "
                f"`hermes config set delegation.backend default`."
            )
        raw_parent = task.get("parent")
        parent: Mapping[str, Any] = raw_parent if isinstance(raw_parent, Mapping) else {}
        role = self._role(task.get("route_role"), settings)
        task_id = f"task-{task.get('index', 0)}-{uuid.uuid4().hex[:8]}"
        timeout = self._timeout(settings)
        decision = self._call_router(argv, role, task.get("author_vendor"), task_id, timeout)
        if decision is None:
            return self._inherit(parent, "router failed")
        if is_blocked(provider=decision.get("provider")):
            logger.warning("delegation router chose blocked provider %r; child inherits the parent",
                           decision.get("provider"))
            return self._inherit(parent, "router chose a blocked provider")
        return {
            "provider": decision["provider"],
            "model": decision["model"],
            "fallback_providers": decision["fallback"],
            "label": decision.get("label"),
            "_router": {
                "decision_id": decision["decision_id"],
                "record_argv": record_argv(argv),
                "reason": decision.get("reason"),
            },
        }

    def task_finished(self, task: Mapping[str, Any], answer: Mapping[str, Any], entry: dict[str, Any]) -> None:
        """Report the settled outcome to the router; never blocks the tool result, never raises."""
        meta = answer.get("_router") if isinstance(answer, Mapping) else None
        if not isinstance(meta, Mapping) or not meta.get("decision_id") or not meta.get("record_argv"):
            return
        outcome = "done" if entry.get("status") == "completed" else "failed"
        argv = list(meta["record_argv"]) + [
            str(meta["decision_id"]), outcome,
            f"provider={answer.get('provider') or ''}", f"model={answer.get('model') or ''}",
        ]
        threading.Thread(target=self._record, args=(argv,), daemon=True).start()

    # -- internals -------------------------------------------------------------------------------------------

    @staticmethod
    def _role(raw: Any, settings: Mapping[str, Any]) -> str:
        role = str(raw or "").strip().lower()
        if role in ROUTER_ROLES:
            return role
        default = str(settings.get("default_role") or "").strip().lower()
        return default if default in ROUTER_ROLES else DEFAULT_ROUTER_ROLE

    @staticmethod
    def _timeout(settings: Mapping[str, Any]) -> int:
        try:
            value = float(settings.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            value = DEFAULT_TIMEOUT_SECONDS
        return int(value) if value > 0 else DEFAULT_TIMEOUT_SECONDS

    def _inherit(self, parent: Mapping[str, Any], reason: str) -> dict[str, Any]:
        """No route: an EMPTY route so core inherits the parent, with its chain filtered and no provider pinned.

        Pinning ``provider`` here would resolve ``"custom"`` for a named custom parent and be refused.
        """
        if is_blocked(provider=parent.get("provider"), requested_provider=parent.get("requested_provider"),
                      base_url=parent.get("base_url")):
            raise DelegationRouteError(
                f"delegation.provider: auto could not route this task ({reason}) and the parent runs on a blocked "
                f"provider ({parent.get('requested_provider') or parent.get('provider')}), which delegated children "
                f"must never inherit. Fix the router (delegation.backends.router.router_command) or pin "
                f"delegation.provider/model explicitly."
            )
        return {
            "fallback_providers": _trim_chain(parent.get("fallback_providers")),
            "label": f"inherited: {reason}",
        }

    @staticmethod
    def _call_router(argv: list[str], role: str, author_vendor: Any, task_id: str,
                     timeout: int) -> Optional[dict[str, Any]]:
        call = argv + [role]
        if author_vendor:
            call += ["--author-vendor", str(author_vendor)]
        call += ["--task-id", task_id]
        try:
            proc = subprocess.run(call, capture_output=True, text=True, timeout=timeout, shell=False,
                                 check=False)
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
        fallback: list[dict[str, Any]] = []
        for entry in data.get("fallback") or []:
            if not isinstance(entry, dict):
                continue
            fb_provider = str(entry.get("provider") or "").strip()
            fb_model = str(entry.get("model") or "").strip()
            if not fb_provider or not fb_model or is_blocked(provider=fb_provider):
                continue
            fallback.append({"provider": fb_provider, "model": fb_model})
        decision_id = data.get("decision_id")
        return {
            "decision_id": str(decision_id) if decision_id not in (None, "") else None,
            "provider": provider,
            "model": model,
            "fallback": fallback,
            "reason": data.get("reason"),
        }

    @staticmethod
    def _record(argv: list[str]) -> None:
        try:
            subprocess.run(argv, capture_output=True, text=True, timeout=RECORD_TIMEOUT_SECONDS, shell=False,
                           check=False)
        except Exception as exc:
            logger.debug("delegation routed-outcome bookkeeping failed: %s", exc)


def register(ctx) -> None:
    ctx.register_delegation_provider(RouterDelegationProvider())
