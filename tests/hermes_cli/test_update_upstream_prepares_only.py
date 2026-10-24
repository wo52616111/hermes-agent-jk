"""``hermes update --upstream`` prepares the merge; applying it is the caller's call.

Moving the checkout restarts the gateway serving it, and that restart is drain-first, so the normal
flow applies the merge. ``--no-gateway-restart`` must NOT move the live tree: it hands back a
ready-to-apply merge in a worktree instead, because the code-change watcher would restart the
gateway anyway.
"""

import subprocess

import pytest

from hermes_cli.update_cmd_git import _integrate_upstream_in_worktree


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
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
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "upstream work")
    _git(fork, "fetch", "-q", "upstream")
    return fork


def test_prepare_only_leaves_the_checkout_untouched(repos, capsys):
    fork = repos
    head = _git(fork, "rev-parse", "HEAD")

    _integrate_upstream_in_worktree(["git"], fork, "upstream/main", apply=False)

    out = capsys.readouterr().out
    assert _git(fork, "rev-parse", "HEAD") == head, "the live checkout must not move without --restart"
    assert _git(fork, "status", "--porcelain") == "", "no half-merge in the live checkout"
    assert "hermes update --upstream" in out, "the printed next step must be a runnable command"
    assert "NOT applied" in out


def test_apply_fast_forwards_the_checkout_and_cleans_up(repos, capsys):
    fork = repos
    heads = _git(fork, "rev-parse", "HEAD")

    _integrate_upstream_in_worktree(["git"], fork, "upstream/main", apply=True)

    assert _git(fork, "rev-parse", "HEAD") != heads
    assert (fork / "f.txt").read_text() == "base\nupstream\n"
    assert _git(fork, "worktree", "list").count("\n") == 0, "the merge worktree is removed after applying"
    assert "fleet restart follows" in capsys.readouterr().out
