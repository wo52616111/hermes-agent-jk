"""delegation.provider: auto — quota-aware per-child routing via an external router command.

Real imports throughout (config loading, credential resolution, child construction wiring, dispatch); only
``subprocess.run`` (the router), the child runner and the AIAgent constructor are replaced.
"""

import json
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from tests.tools.test_delegate import _make_mock_parent
from tools.delegate_tool import DELEGATE_TASK_SCHEMA, delegate_task
from tools.delegate_tool_routing import DEFAULT_ROUTER_COMMAND, record_argv

ROUTER = "python3 /opt/eva-model.py route --json"
DECISION = {
    "decision_id": "d-123", "family": "anthropic", "provider": "openrouter", "model": "anthropic/claude-opus-4",
    "fallback": [
        {"family": "openai", "provider": "openrouter", "model": "openai/gpt-5"},
        {"family": "openai", "provider": "aftership-codex-proxy", "model": "gpt-5-codex"},
        {"family": "google", "provider": "openrouter", "model": None},
        {"family": "deepseek", "provider": "deepseek", "model": "deepseek-chat"},
    ],
    "reason": "anthropic quota healthy",
}
EXPECTED_FALLBACK = [
    {"provider": "openrouter", "model": "openai/gpt-5"},
    {"provider": "deepseek", "model": "deepseek-chat"},
]


@pytest.fixture(autouse=True)
def _provider_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-routing")


def _parent():
    parent = _make_mock_parent(depth=0)
    parent._fallback_chain = [{"provider": "nous", "model": "parent-fallback"}]
    parent.request_overrides = {}
    return parent


class _Router:
    """subprocess.run stand-in: answers ``route`` calls, records every argv."""

    def __init__(self, route_result):
        self.route_result = route_result
        self.calls = []
        self.recorded = threading.Event()

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if "record" in argv:
            self.recorded.set()
            return subprocess.CompletedProcess(argv, 0, "", "")
        if isinstance(self.route_result, BaseException):
            raise self.route_result
        return self.route_result

    def route_calls(self):
        return [a for a, _ in self.calls if "route" in a]

    def record_calls(self):
        assert self.recorded.wait(5), "record was never fired"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any("record" in a for a, _ in self.calls):
            time.sleep(0.01)
        return [a for a, _ in self.calls if "record" in a]


def _ok(payload=DECISION):
    return subprocess.CompletedProcess([], 0, json.dumps(payload), "")


def _run(cfg, router, tasks, child_entry=None, child_raises=None, parent=None):
    """delegate_task end-to-end (sync) → (result dict, AIAgent kwargs per child)."""
    child_entry = child_entry or {"status": "completed", "summary": "ok", "api_calls": 1, "duration_seconds": 0.1}

    def _fake_child_runner(task_index, goal, child=None, parent_agent=None, **_kw):
        if child_raises is not None:
            raise child_raises
        return {"task_index": task_index, **child_entry}

    built = []

    def _fake_agent(**kwargs):
        child = MagicMock()
        child.session_id = f"child-sess-{len(built)}"
        child.provider, child.model = kwargs.get("provider"), kwargs.get("model")
        built.append(kwargs)
        return child

    with patch("tools.delegate_tool._load_config", return_value=cfg), \
            patch("tools.delegate_tool_config._load_config", return_value=cfg), \
            patch("subprocess.run", side_effect=router), \
            patch("run_agent.AIAgent", side_effect=_fake_agent), \
            patch("tools.delegate_tool._run_single_child", side_effect=_fake_child_runner):
        raw = delegate_task(tasks=tasks, parent_agent=parent or _parent())
    return json.loads(raw), built


def test_auto_routes_each_child_and_pins_router_choice():
    router = _Router(_ok())
    cfg = {"provider": "auto", "router_command": ROUTER,
           "fallback_providers": [{"provider": "nous", "model": "ignored-static-fallback"}]}
    result, built = _run(cfg, router, [{"goal": "review the diff", "route_role": "review", "author_vendor": "openai"}])

    routes = router.route_calls()
    assert len(routes) == 1
    argv = routes[0]
    assert argv[:4] == ["python3", "/opt/eva-model.py", "route", "--json"]
    assert argv[4] == "review"
    assert argv[5:7] == ["--author-vendor", "openai"]
    assert argv[7] == "--task-id" and argv[8].startswith("sa-0-")
    assert all(kw.get("shell", False) is False and kw["timeout"] == 15 for a, kw in router.calls if "route" in a)

    (kwargs,) = built
    assert kwargs["provider"] == "openrouter"
    assert kwargs["model"] == "anthropic/claude-opus-4"
    assert kwargs["api_key"] == "sk-or-test-routing"
    assert "openrouter" in (kwargs["base_url"] or "")
    # Exactly the router's chain: model-null skipped, proxy stripped, static delegation.fallback_providers ignored.
    assert kwargs["fallback_model"] == EXPECTED_FALLBACK

    entry = result["results"][0]
    assert entry["routed"] == {"decision_id": "d-123", "provider": "openrouter",
                               "model": "anthropic/claude-opus-4", "reason": "anthropic quota healthy"}
    (rec,) = router.record_calls()
    assert rec == ["python3", "/opt/eva-model.py", "record", "d-123", "done", "session=child-sess-0",
                   "provider=openrouter", "model=anthropic/claude-opus-4"]


def test_route_role_selects_lane_independently_of_capability_role():
    router = _Router(_ok())
    _run({"provider": "auto", "router_command": ROUTER}, router,
         [{"goal": "review the diff", "route_role": "review", "role": "leaf"}])
    assert router.route_calls()[0][4] == "review"

    router = _Router(_ok())
    _run({"provider": "auto", "router_command": ROUTER}, router, [{"goal": "review the diff", "role": "review"}])
    assert router.route_calls()[0][4] == "code-core"  # `role` never selects a routing lane


def test_review_child_passes_no_prompt_file():
    router = _Router(_ok())
    _run({"provider": "auto", "router_command": ROUTER}, router,
         [{"goal": "review the parser diff", "context": "diff", "route_role": "review"}])
    assert "--prompt-file" not in router.route_calls()[0]


def test_role_defaults_to_code_core_and_task_id_matches_subagent():
    router = _Router(_ok())
    _run({"provider": "auto", "router_command": ROUTER}, router, [{"goal": "implement the parser"}])
    argv = router.route_calls()[0]
    assert argv[4] == "code-core"
    assert "--author-vendor" not in argv


def test_failed_child_records_failed():
    router = _Router(_ok())
    result, _ = _run({"provider": "auto", "router_command": ROUTER}, router, [{"goal": "implement the parser"}],
                     child_entry={"status": "failed", "summary": None, "error": "boom", "api_calls": 1,
                                  "duration_seconds": 0.1})
    assert result["results"][0]["routed"]["decision_id"] == "d-123"
    (rec,) = router.record_calls()
    assert rec[3:5] == ["d-123", "failed"]


def test_child_runner_crash_still_records_failed():
    # Two tasks → parallel path, which converts a runner crash into an error entry (the single-task path re-raises).
    router = _Router(_ok())
    result, _ = _run({"provider": "auto", "router_command": ROUTER}, router,
                     [{"goal": "implement the parser module"}, {"goal": "implement the lexer module"}],
                     child_raises=RuntimeError("runner exploded"))
    assert [e["status"] for e in result["results"]] == ["error", "error"]
    router.record_calls()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len([a for a, _ in router.calls if "record" in a]) < 2:
        time.sleep(0.01)
    recs = [a for a, _ in router.calls if "record" in a]
    assert len(recs) == 2 and all(r[3:5] == ["d-123", "failed"] for r in recs)


@pytest.mark.parametrize("route_result", [
    subprocess.CompletedProcess([], 2, "", "quota db locked"),
    subprocess.TimeoutExpired(cmd="router", timeout=15),
    FileNotFoundError("python3"),
    subprocess.CompletedProcess([], 0, "not json", ""),
    subprocess.CompletedProcess([], 0, json.dumps({**DECISION, "model": None}), ""),
    subprocess.CompletedProcess([], 0, json.dumps({**DECISION, "provider": "no-such-provider-xyz"}), ""),
    subprocess.CompletedProcess([], 0, json.dumps({**DECISION, "provider": "aftership-codex-proxy"}), ""),
], ids=["nonzero", "timeout", "missing", "bad-json", "model-null", "unresolvable", "blocked-proxy"])
def test_router_failure_inherits_parent(route_result, caplog):
    router = _Router(route_result)
    result, built = _run({"provider": "auto", "router_command": ROUTER}, router, [{"goal": "implement the parser"}])
    assert result["results"][0]["status"] == "completed"
    (kwargs,) = built
    assert kwargs["provider"] == "openrouter"           # the parent's
    assert kwargs["model"] == "anthropic/claude-sonnet-4"  # the parent's
    assert kwargs["fallback_model"] == [{"provider": "nous", "model": "parent-fallback"}]  # inherited chain
    assert "routed" not in result["results"][0]
    assert not any("record" in a for a, _ in router.calls)
    assert any("router" in r.getMessage() for r in caplog.records if r.levelname == "WARNING")


def test_router_failure_with_proxy_parent_fails_delegation():
    router = _Router(subprocess.CompletedProcess([], 2, "", "quota db locked"))
    parent = _parent()
    parent.provider = "aftership-codex-proxy"
    result, built = _run({"provider": "auto", "router_command": ROUTER}, router,
                         [{"goal": "implement the parser"}], parent=parent)
    assert built == []
    assert "aftership-codex-proxy" in result["error"]
    assert "delegation.provider: auto" in result["error"]


def test_router_failure_strips_blocked_provider_from_inherited_fallback():
    router = _Router(subprocess.CompletedProcess([], 2, "", "quota db locked"))
    parent = _parent()
    parent._fallback_chain = [{"provider": "aftership-codex-proxy", "model": "gpt-5-codex"},
                              {"provider": "nous", "model": "parent-fallback"}]
    _, built = _run({"provider": "auto", "router_command": ROUTER}, router,
                    [{"goal": "implement the parser"}], parent=parent)
    (kwargs,) = built
    assert kwargs["fallback_model"] == [{"provider": "nous", "model": "parent-fallback"}]


def test_router_failure_with_only_blocked_fallback_disables_fallback():
    router = _Router(subprocess.CompletedProcess([], 2, "", "quota db locked"))
    parent = _parent()
    parent._fallback_chain = [{"provider": "aftership-codex-proxy", "model": "gpt-5-codex"}]
    _, built = _run({"provider": "auto", "router_command": ROUTER,
                     "fallback_providers": [{"provider": "aftership-codex-proxy", "model": "gpt-5-codex"}]},
                    router, [{"goal": "implement the parser"}], parent=parent)
    (kwargs,) = built
    assert not kwargs["fallback_model"]


def test_child_end_refreshes_quota_for_the_child_provider(monkeypatch):
    from agent import quota_state

    refreshed = threading.Event()
    seen = []
    monkeypatch.setattr(quota_state, "refresh",
                        lambda provider, **_k: seen.append(provider) or refreshed.set() or "fetched")
    router = _Router(_ok({**DECISION, "provider": "anthropic", "model": "claude-opus-4"}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "«redacted:sk-…»")
    _run({"provider": "auto", "router_command": ROUTER}, router, [{"goal": "implement the parser"}])
    assert refreshed.wait(5)
    assert seen == ["anthropic"]


def test_non_auto_config_never_calls_router():
    router = _Router(_ok())
    result, built = _run({"router_command": ROUTER}, router, [{"goal": "implement the parser", "route_role": "review"}])
    assert router.calls == []
    assert built[0]["model"] == "anthropic/claude-sonnet-4"
    assert "routed" not in result["results"][0]

    router = _Router(_ok())
    result, built = _run({"provider": "openrouter", "model": "static/model", "router_command": ROUTER}, router,
                         [{"goal": "implement the parser"}])
    assert router.calls == []
    assert built[0]["model"] == "static/model"


def test_record_argv_derivation():
    assert record_argv({"router_command": ROUTER}) == ["python3", "/opt/eva-model.py", "record"]
    assert record_argv({"router_command": "my-router --json pick"}) is None
    default = record_argv({})
    assert default is not None
    assert default[-1] == "record" and "--json" not in default and "~" not in " ".join(default)
    assert DEFAULT_ROUTER_COMMAND.endswith("route --json")


def test_schema_advertises_routing_fields():
    props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert props["route_role"]["enum"] == ["code-core", "code-routine", "screen", "ideate", "review"]
    assert "role" not in props
    assert props["author_vendor"]["type"] == "string"
