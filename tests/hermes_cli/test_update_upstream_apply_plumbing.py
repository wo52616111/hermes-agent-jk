"""`--upstream` alone applies the merge; `--no-gateway-restart` only prepares it.

Both places that reconcile upstream during an update must carry that intent: the plan-time check
(the checkout is already at origin/main) and the pull-time one (origin just moved). A call site that
drops it silently downgrades `hermes update --upstream` to prepare-only, which is what left a
resolved merge stranded in a worktree instead of landing on the fork's main.
"""

import subprocess
from pathlib import Path

import pytest

import hermes_cli.main as hermes_main
import hermes_cli.update_cmd as uc


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def fork(tmp_path, monkeypatch):
    """A fork clone whose origin is up to date, with an upstream remote that has one extra commit."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path / "fork")

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "t")
    (upstream / "f.txt").write_text("base\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "base")

    fork = tmp_path / "fork"
    _git(tmp_path, "clone", "-q", str(upstream), str(fork))
    _git(fork, "config", "user.email", "t@example.com")
    _git(fork, "config", "user.name", "t")
    _git(fork, "remote", "add", "upstream", str(upstream))

    (upstream / "f.txt").write_text("base\nupstream\n")
    _git(upstream, "commit", "-qam", "upstream work")
    _git(fork, "fetch", "-q", "upstream")
    return fork


@pytest.fixture
def fork_origin_ahead(fork):
    """The fork's own published line moved (origin/main gained a commit): the pull-time path runs."""
    origin = fork.parent / "upstream"  # the directory the fork was cloned from
    (origin / "g.txt").write_text("origin\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "origin work")
    _git(fork, "fetch", "-q", "origin")
    return fork


def _capture_sync(monkeypatch):
    seen = {}

    def fake_sync(git_cmd, cwd, **kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(hermes_main, "_sync_with_upstream_if_needed", fake_sync)
    return seen


@pytest.mark.parametrize("apply", [True, False])
def test_the_plan_time_check_carries_the_apply_intent(fork, monkeypatch, apply):
    seen = _capture_sync(monkeypatch)

    uc._prepare_checkout_for_update(
        ["git"], "main", "main", is_fork=True, assume_yes=True, gateway_mode=False, gw_input_fn=None,
        switch_branch=False, _windows_gateway_resume=None, upstream_sync=True, upstream_apply=apply)

    assert seen.get("upstream_requested") is True
    assert seen.get("upstream_apply") is apply, "the plan-time check must not drop the caller's intent"


@pytest.mark.parametrize("apply", [True, False])
def test_the_pull_time_check_carries_the_apply_intent(fork_origin_ahead, monkeypatch, apply):
    seen = _capture_sync(monkeypatch)

    uc._pull_updates(
        ["git"], "main", None, prompt_for_restore=False, gw_input_fn=None, discard_local_changes=False,
        keep_stash=False, sync_upstream=True, assume_yes=True, upstream_apply=apply)

    assert seen.get("upstream_requested") is True
    assert seen.get("upstream_apply") is apply, "the pull-time check must not drop the caller's intent"
