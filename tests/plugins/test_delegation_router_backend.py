"""``delegation.backend: router`` — the external quota router as a delegation backend.

Real imports throughout; only the router subprocess and the credential resolver are replaced.
"""

from __future__ import annotations

import json
import subprocess
import time
from unittest.mock import patch

import pytest

from agent.delegation_provider import DelegationRouteError
from plugins.delegation.router import (
    BLOCKED_HOSTS, BLOCKED_PROVIDERS, ROUTER_ROLES, RouterDelegationProvider, record_argv, unresolvable_token,
)

DECISION = {
    "decision_id": "d-123", "family": "anthropic", "provider": "openrouter", "model": "anthropic/claude-opus-4",
    "fallback": [
        {"provider": "openrouter", "model": "openai/gpt-5"},
        {"provider": "aftership-codex-proxy", "model": "gpt-5-codex"},
        {"provider": "openrouter", "model": None},
        {"provider": "deepseek", "model": "deepseek-chat"},
    ],
    "reason": "anthropic quota healthy",
}
CLEAN_FALLBACK = [{"provider": "openrouter", "model": "openai/gpt-5"},
                  {"provider": "deepseek", "model": "deepseek-chat"}]
PARENT = {"provider": "custom", "requested_provider": "open-design", "model": "deepseek-v4.1-flash",
          "base_url": "https://open-design.example/v1",
          "fallback_providers": [{"provider": "nous", "model": "parent-fallback"},
                                 {"provider": "aftership-codex-proxy", "model": "x"}]}


def _ok(payload=None):
    return subprocess.CompletedProcess([], 0, json.dumps(payload if payload is not None else DECISION), "")


class _Router:
    def __init__(self, result=None):
        self.result = result if result is not None else _ok()
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
def provider():
    return RouterDelegationProvider()


def test_name_and_summary(provider):
    assert provider.name == "router"
    assert "router" in provider.summary.lower() or "quota" in provider.summary.lower()
    assert provider.is_available() is True


def test_settings_come_from_the_backend_block(provider):
    cfg = {"backend": "router", "backends": {"router": {"router_command": "python3 /opt/r.py route --json",
                                                         "timeout_seconds": 3}}}
    assert provider.settings(cfg)["timeout_seconds"] == 3


def test_task_parameters_declare_both_hints(provider):
    params = provider.task_parameters({})
    assert params["route_role"]["enum"] == list(ROUTER_ROLES)
    assert "opencode" in params["author_vendor"]["enum"]  # a deepseek-authored review must still be nameable
    assert all(not value.startswith("--") for value in params["author_vendor"]["enum"])


def test_route_task_answers_with_the_router_choice(provider, tmp_path):
    script = tmp_path / "eva-model.py"
    script.write_text("#!/usr/bin/env python3\n")
    router = _Router()
    cfg = {"backends": {"router": {"router_command": f"python3 {script} route --json"}}}
    task = {"goal": "review the diff", "index": 0, "route_role": "review", "author_vendor": "openai",
            "parent": PARENT}
    with patch("subprocess.run", side_effect=router):
        route = provider.route_task(task, cfg)
    assert (route["provider"], route["model"]) == ("openrouter", "anthropic/claude-opus-4")
    assert route["fallback_providers"] == CLEAN_FALLBACK  # proxy + null-model entries dropped
    assert route["_router"]["decision_id"] == "d-123"
    assert route["_router"]["record_argv"] == ["python3", str(script), "record"]
    argv = router.calls[0][0]
    assert argv[4] == "review" and argv[5:7] == ["--author-vendor", "openai"]
    assert argv[7] == "--task-id" and argv[8].startswith("task-0-")


def test_missing_router_command_refuses_and_names_it(provider):
    cfg = {"backends": {"router": {"router_command": "python3 /opt/nope/eva-model.py route --json"}}}
    with pytest.raises(DelegationRouteError) as excinfo:
        provider.route_task({"goal": "x", "index": 0, "parent": PARENT}, cfg)
    assert "/opt/nope/eva-model.py" in str(excinfo.value)


def test_a_router_script_argument_needs_no_executable_bit(tmp_path):
    script = tmp_path / "eva-model.py"
    script.write_text("#!/usr/bin/env python3\n")
    assert unresolvable_token(["python3", str(script), "route"]) is None
    assert unresolvable_token(["python3", str(tmp_path / "gone.py"), "route"]) == str(tmp_path / "gone.py")


@pytest.mark.parametrize("result", [subprocess.CompletedProcess([], 3, "", "boom"),
                                    subprocess.CompletedProcess([], 0, "{not json", ""),
                                    subprocess.CompletedProcess([], 0, json.dumps({"provider": "x"}), ""),
                                    TimeoutError("slow")])
def test_router_trouble_inherits_the_parent(provider, result):
    router = _Router(result)
    with patch("subprocess.run", side_effect=router):
        route = provider.route_task({"goal": "x", "index": 0, "parent": PARENT}, {})
    assert "provider" not in route and "model" not in route  # never pins: a named custom parent would be refused
    assert route["fallback_providers"] == [{"provider": "nous", "model": "parent-fallback"}]
    assert route["label"] == "inherited: router failed"


def test_blocked_answer_inherits_instead(provider):
    router = _Router(_ok({**DECISION, "provider": "aftership-codex-proxy"}))
    with patch("subprocess.run", side_effect=router):
        route = provider.route_task({"goal": "x", "index": 0, "parent": PARENT}, {})
    assert "provider" not in route
    assert route["label"] == "inherited: router chose a blocked provider"


def test_blocked_parent_refuses_the_task(provider):
    router = _Router(subprocess.CompletedProcess([], 1, "", "down"))
    blocked_parent = {**PARENT, "provider": "custom", "requested_provider": "aftership-codex-proxy"}
    with patch("subprocess.run", side_effect=router):
        with pytest.raises(DelegationRouteError) as excinfo:
            provider.route_task({"goal": "x", "index": 0, "parent": blocked_parent}, {})
    assert "blocked" in str(excinfo.value).lower()


def test_blocked_parent_by_host_only(provider):
    router = _Router(subprocess.CompletedProcess([], 1, "", "down"))
    host_parent = {**PARENT, "provider": "custom", "requested_provider": None,
                   "base_url": f"https://{min(BLOCKED_HOSTS)}/v1"}
    with patch("subprocess.run", side_effect=router):
        with pytest.raises(DelegationRouteError):
            provider.route_task({"goal": "x", "index": 0, "parent": host_parent}, {})


def test_task_finished_reports_the_decision(provider):
    answer = {"provider": "openrouter", "model": "m",
              "_router": {"decision_id": "d-9", "record_argv": ["python3", "/opt/eva-model.py", "record"]}}
    calls = []

    def _run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    with patch("subprocess.run", side_effect=_run):
        provider.task_finished({"goal": "x"}, answer, {"status": "completed"})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not calls:
            time.sleep(0.01)
    assert calls and calls[0][3:] == ["d-9", "done", "provider=openrouter", "model=m"]


def test_task_finished_ignores_an_unrouted_answer(provider):
    with patch("subprocess.run") as run:
        provider.task_finished({}, {"provider": "openrouter"}, {"status": "completed"})
    run.assert_not_called()


def test_record_argv_derivation():
    assert record_argv(["python3", "/x.py", "route", "--json"]) == ["python3", "/x.py", "record"]
    assert record_argv(["python3", "/x.py", "pick"]) is None


def test_blocked_constants_are_not_empty():
    assert "aftership-codex-proxy" in BLOCKED_PROVIDERS and BLOCKED_HOSTS
