"""The macOS (launchd) update restart must drain first — not kickstart straight into a kill.

`launchctl kickstart -k` is a planned stop: its drain budget is `agent.restart_drain_timeout`, whose
default is 0s, so it interrupts running turns and background delegations after 0.00s. The systemd path
already sends SIGUSR1 (→ `request_restart(via_service=True)`) first; launchd must do the same.
"""

import hermes_cli.gateway as gateway_cli
import hermes_cli.update_cmd_fleet as fleet


def test_launchd_restart_drains_before_kickstart(monkeypatch, tmp_path):
    plist = tmp_path / "ai.hermes.gateway.plist"
    plist.write_text("<plist/>")
    order: list = []

    monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway_cli, "_launchctl_supervised_pid", lambda label: 4242)
    monkeypatch.setattr(gateway_cli, "_is_pid_ancestor_of_current_process", lambda pid: False)
    monkeypatch.setattr(gateway_cli, "wait_for_launchd_gateway_supervision", lambda **kw: True)

    def _kickstart():
        order.append(("kickstart",))

    monkeypatch.setattr(gateway_cli, "launchd_restart", _kickstart)

    def _drain(pid, budget, label, *, self_restart_pending=None):
        order.append(("drain", pid, budget, label))
        return True

    monkeypatch.setattr(fleet, "_drain_or_signal_gateway_for_update", _drain)
    monkeypatch.setattr(fleet, "_launchd_drain_budget", lambda: 1860.0)

    restarted, failed = fleet._restart_launchd_gateway_after_update(supervision_verify=False)

    assert restarted == ["ai.hermes.gateway"] and failed == []
    assert order[0] == ("drain", 4242, 1860.0, "ai.hermes.gateway"), "drain must come first"
    assert order[1] == ("kickstart",), "the forced restart still runs after the drain"


def test_drain_budget_outlasts_the_gateway_after_turn_cap(monkeypatch):
    """A gateway waiting its full `restart_after_turn_timeout` must not be force-killed at 45s."""
    monkeypatch.setattr(gateway_cli, "_get_restart_exit_wait_budget", lambda: 45.0)
    budget = fleet._launchd_drain_budget()
    assert budget >= 1800.0, f"expected the after-turn cap plus slack, got {budget}"
