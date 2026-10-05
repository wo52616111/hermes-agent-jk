"""``session.usage`` RPC (Desktop usage feed) carries the provider account-limits block.

The CLI/TUI slash worker and gateway ``/usage`` render Codex quota windows via
``render_account_usage_lines``; the Desktop feed reads ``session.usage`` instead, so the RPC
must ship the same lines (``account_lines``) or that surface silently omits them.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow


def _snapshot(provider: str) -> AccountUsageSnapshot:
    return AccountUsageSnapshot(
        provider=provider, source="usage_api", fetched_at=datetime.now(timezone.utc), plan="Plus",
        windows=(AccountUsageWindow(label="Weekly", used_percent=12.0),),
    )


def test_session_usage_rpc_ships_account_lines_for_the_live_route():
    from tui_gateway import server

    agent = SimpleNamespace(provider="openrouter", base_url="https://openrouter.example/api/v1",
                            api_key="tok", model="openai/gpt-5")
    session = {"agent": agent, "history": [], "running": False, "session_key": "sess-usage"}
    sid = "sid-usage-account"
    server._sessions[sid] = session
    seen: list[tuple] = []

    def _fetch(provider, *, base_url=None, api_key=None):
        seen.append((provider, base_url, api_key))
        return _snapshot("openrouter")

    try:
        with (
            patch.object(server, "_get_usage", return_value={"calls": 1, "input": 10, "output": 20, "total": 30}),
            patch("agent.account_usage.fetch_account_usage", _fetch),
            patch("agent.account_usage.nous_credits_lines", lambda **kw: []),
        ):
            r = server._methods["session.usage"]("r1", {"session_id": sid})
    finally:
        server._sessions.pop(sid, None)

    assert "error" not in r, r
    result = r["result"]
    assert result["total"] == 30 and "credits_lines" not in result
    # Fetched against the session's own route, not a default endpoint.
    assert seen == [("openrouter", "https://openrouter.example/api/v1", "tok")]
    assert "Provider: openrouter (Plus)" in result["account_lines"]
    assert any(line.startswith("Weekly") and "12%" in line for line in result["account_lines"])


def test_session_usage_rpc_serves_subscription_lines_from_shared_quota_state(monkeypatch, tmp_path):
    """Subscription providers read the shared snapshot (one fetch across processes, 429 backoff
    honoured) instead of hitting the usage endpoint on every ``session.usage`` call."""
    from agent import quota_state
    from tui_gateway import server

    monkeypatch.setattr(quota_state, "quota_dir", lambda: tmp_path / "quota")
    monkeypatch.setattr(quota_state, "default_connected", lambda provider: True)
    monkeypatch.setattr(quota_state, "default_multi_account", lambda provider: False)
    fetched = []
    monkeypatch.setattr(quota_state, "default_fetcher", lambda p: fetched.append(p) or _snapshot("openai-codex"))
    quota_state._connected_cache.clear()

    def _direct(*_a, **_k):
        raise AssertionError("session.usage must not fetch account usage directly")

    agent = SimpleNamespace(provider="openai-codex", base_url="https://chatgpt.example/backend-api",
                            api_key="tok", model="gpt-5.3-codex")
    sid = "sid-usage-shared"
    server._sessions[sid] = {"agent": agent, "history": [], "running": False, "session_key": "sess-shared"}
    try:
        with (
            patch.object(server, "_get_usage", return_value={"calls": 1, "input": 10, "output": 20, "total": 30}),
            patch("agent.account_usage.fetch_account_usage", _direct),
            patch("agent.account_usage.nous_credits_lines", lambda **kw: []),
        ):
            first = server._methods["session.usage"]("r1", {"session_id": sid})["result"]
            second = server._methods["session.usage"]("r2", {"session_id": sid})["result"]
    finally:
        server._sessions.pop(sid, None)

    assert fetched == ["openai-codex"]
    for result in (first, second):
        assert any("openai-codex" in line for line in result["account_lines"])
        assert any(line.startswith("Current week") and "12%" in line for line in result["account_lines"])
