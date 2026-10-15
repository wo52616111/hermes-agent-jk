"""An enabled MCP server named in ``toolsets`` must not fail session creation while it is offline.

The CLI sends its resolved toolset list, MCP server names included. A server only registers as a
toolset once it connects, so one server with an expired OAuth token used to make the owner refuse
the whole ``session.create`` with ``invalid_params``.
"""
import pytest

from gateway.session_policy import _resolve_toolsets
from hermes_state_runtime import RuntimeStoreError

OFFLINE = "offline-oauth-server"


def _config(**servers):
    return {"mcp_servers": servers}


def test_enabled_but_unconnected_mcp_server_is_accepted():
    config = _config(**{OFFLINE: {"url": "https://example.invalid/mcp", "auth": "oauth"}})
    enabled = _resolve_toolsets({"toolsets": ["file", OFFLINE]}, config, "oneshot", False)
    assert OFFLINE in enabled
    assert "file" in enabled


def test_unknown_name_that_is_not_a_configured_server_is_still_refused():
    config = _config(**{OFFLINE: {"url": "https://example.invalid/mcp"}})
    with pytest.raises(RuntimeStoreError):
        _resolve_toolsets({"toolsets": ["file", "typo-not-a-toolset"]}, config, "oneshot", False)


def test_disabled_mcp_server_name_is_still_refused():
    config = _config(**{OFFLINE: {"url": "https://example.invalid/mcp", "enabled": False}})
    with pytest.raises(RuntimeStoreError):
        _resolve_toolsets({"toolsets": [OFFLINE]}, config, "oneshot", False)
