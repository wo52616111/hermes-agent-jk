"""A refused ``prompt.submit`` must leave a server-side trace.

The client can only paint "delivery unconfirmed" and then refuses to resend an identityless write, so a
stuck queue used to be invisible in the logs: no way to tell a busy owner from a dead one afterwards.
"""

import logging
from types import SimpleNamespace

from tui_gateway import methods_prompt


def test_refusal_is_logged_with_the_verdict_and_the_ledger_state(caplog):
    session = {"session_key": "agent:main:local:dm:local-abc"}
    refusal = SimpleNamespace(reason="SESSION_NOT_OWNED", ledger_status="started")

    with caplog.at_level(logging.WARNING, logger="tui_gateway.methods_prompt"):
        methods_prompt._log_submit_refusal("ui-1", session, refusal,
                                           reason="SESSION_OWNER_NOT_RUNNING", detail="no owner")

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "prompt.submit refused (SESSION_OWNER_NOT_RUNNING)" in message
    assert "session=agent:main:local:dm:local-abc" in message
    assert "ledger=started" in message, "the ledger row's own status is what explains the refusal"
    assert "owner=" in message


def test_the_log_line_survives_an_unreadable_owner_probe(caplog, monkeypatch):
    """Diagnostics must never raise out of the refusal path they describe."""
    from tui_gateway import owner_forward

    def boom(*_a, **_k):
        raise RuntimeError("owner probe exploded")

    monkeypatch.setattr(owner_forward, "discover_owner", boom)
    with caplog.at_level(logging.WARNING, logger="tui_gateway.methods_prompt"):
        methods_prompt._log_submit_refusal("ui-2", {"session_key": "k"},
                                           SimpleNamespace(ledger_status="queued"),
                                           reason="SESSION_OWNER_BUSY", detail="images")

    assert "owner=unprobed" in caplog.records[0].getMessage()
