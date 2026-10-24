"""The child's own quota refreshes when its run ends (survives the router moving into a backend plugin)."""

from types import SimpleNamespace
from unittest.mock import patch

from tools.delegate_tool_dispatch import _refresh_child_quota


def test_child_end_refreshes_quota_for_the_child_provider():
    child = SimpleNamespace(provider="anthropic", base_url="https://api.anthropic.com")
    with patch("agent.quota_state.refresh", return_value="fetched") as refresh:
        _refresh_child_quota(child)
    refresh.assert_called_once()
    assert refresh.call_args.args[0] == "anthropic"


def test_a_child_without_a_provider_refreshes_nothing():
    with patch("agent.quota_state.refresh") as refresh:
        _refresh_child_quota(SimpleNamespace(provider=None, base_url=None))
    refresh.assert_not_called()
