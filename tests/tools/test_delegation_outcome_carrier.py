"""The two carrier surfaces a delegation backend needs to report outcomes: ``TaskRoute.answer`` (per task) and the
duck-typed ``task_finished(task, answer, entry)`` hook fired from result finalization.

Real imports; the provider and the credential resolver are stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.delegation_provider import DelegationProvider
from tools.delegate_tool_results import _fire_delegation_outcomes
from tools.delegate_tool_routes import TaskRoute, _parent_context, _route_to_task_route, resolve_task_routes


class _OutcomeProvider(DelegationProvider):
    """Answers identically for every task but keeps a per-call decision id — the cache must not merge them."""

    def __init__(self):
        self.finished = []

    @property
    def name(self):
        return "router"

    def route_task(self, task, config):
        return {"provider": "openrouter", "model": "anthropic/claude-opus-4", "fallback_providers": [],
                "decision_id": f"d-{task['index']}"}

    def task_finished(self, task, answer, entry):
        self.finished.append((task, answer, entry))


def _parent():
    return SimpleNamespace(provider="openrouter", requested_provider="openrouter", model="parent-model",
                           base_url="https://example.test/v1",
                           _fallback_chain=[{"provider": "nous", "model": "fallback", "api_key": "sk-secret"}])


def test_parent_context_carries_identity_and_a_trimmed_chain():
    ctx = _parent_context(_parent())
    assert ctx["provider"] == "openrouter" and ctx["requested_provider"] == "openrouter"
    assert ctx["model"] == "parent-model" and ctx["base_url"] == "https://example.test/v1"
    assert ctx["fallback_providers"] == [{"provider": "nous", "model": "fallback"}]  # no api_key


def test_identical_answers_keep_their_own_decision_id(monkeypatch):
    provider = _OutcomeProvider()
    monkeypatch.setattr("agent.delegation_registry.get_active_provider", lambda config: provider)
    monkeypatch.setattr("tools.delegate_tool_routes._resolve_route",
                        lambda route, parent_agent, backend: TaskRoute({"model": "m"}, {"fallback_providers": []}))

    routes, err = resolve_task_routes([{"goal": "a"}, {"goal": "b"}], {"backend": "router"}, _parent())

    assert err is None
    answers = [r.answer for r in routes]
    assert all(isinstance(a, dict) for a in answers)
    assert [a["raw"]["decision_id"] for a in answers] == ["d-0", "d-1"]
    assert all(a["provider"] is provider for a in answers)


def test_a_backend_without_outcomes_carries_no_answer(monkeypatch):
    monkeypatch.setattr("agent.delegation_registry.get_active_provider", lambda config: _NoOutcomeProvider())
    monkeypatch.setattr("tools.delegate_tool_routes._resolve_route",
                        lambda route, parent_agent, backend: TaskRoute({"model": "m"}, {}))

    routes, err = resolve_task_routes([{"goal": "a"}], {}, _parent())

    assert err is None and routes[0].answer is None


class _NoOutcomeProvider(DelegationProvider):
    @property
    def name(self):
        return "default"

    def route_task(self, task, config):
        return {}


def test_the_hook_fires_once_per_settled_entry():
    provider = _OutcomeProvider()
    answer = {"provider": provider, "raw": {"decision_id": "d-7"}}
    child = SimpleNamespace(_delegate_answer=answer)
    results = [{"task_index": 0, "status": "completed", "summary": "ok"}]

    _fire_delegation_outcomes(results, [{"goal": "a"}], {0: child})

    assert len(provider.finished) == 1
    task, raw, entry = provider.finished[0]
    assert task == {"goal": "a"} and raw == {"decision_id": "d-7"} and entry is results[0]


def test_the_hook_never_raises_into_the_result():
    class _Boom(DelegationProvider):
        @property
        def name(self):
            return "boom"

        def route_task(self, task, config):
            return {}

        def task_finished(self, task, answer, entry):
            raise RuntimeError("bookkeeping blew up")

    child = SimpleNamespace(_delegate_answer={"provider": _Boom(), "raw": {}})
    results = [{"task_index": 0, "status": "failed"}]

    _fire_delegation_outcomes(results, [{"goal": "a"}], {0: child})  # must not raise

    assert results[0]["status"] == "failed"
