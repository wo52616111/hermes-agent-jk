"""``hermes update --upstream`` prepares the merge; applying it is the caller's call.

Moving the checkout restarts the gateway serving it, and that restart is drain-first, so the normal
flow applies the merge. ``--no-gateway-restart`` must NOT move the live tree: it hands back a
ready-to-apply merge in a worktree instead, because the code-change watcher would restart the
gateway anyway.
"""

import subprocess
from pathlib import Path

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


@pytest.fixture
def diverged(tmp_path, monkeypatch):
    """A fork whose own commit touches the same line upstream moved: the merge must conflict."""
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

    (fork / "f.txt").write_text("base\nfork\n")
    _git(fork, "commit", "-qam", "fork work")

    (upstream / "f.txt").write_text("base\nupstream\n")
    _git(upstream, "commit", "-qam", "upstream work")
    _git(fork, "fetch", "-q", "upstream")
    return fork


def _sync_worktrees(fork):
    """Every worktree this mechanism created, in listing order."""
    entries = [block for block in _git(fork, "worktree", "list", "--porcelain").split("\n\n") if block.strip()]
    return [block for block in entries if "upstream-sync" in block]


def test_a_resolved_conflict_merge_is_applied_by_the_next_run(diverged, capsys):
    """The whole point of the worktree: resolving the conflicts there must reach the checkout."""
    fork = diverged
    before = _git(fork, "rev-parse", "HEAD")

    _integrate_upstream_in_worktree(["git"], fork, "upstream/main", apply=False)

    assert "Conflicts in 1 file(s)" in capsys.readouterr().out
    assert _git(fork, "rev-parse", "HEAD") == before, "a conflicted merge never moves the checkout"
    worktrees = _sync_worktrees(fork)
    assert len(worktrees) == 1
    wt = Path(worktrees[0].splitlines()[0].split(" ", 1)[1])

    # Resolve it the way the message says.
    (wt / "f.txt").write_text("base\nfork\nupstream\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "--no-edit")
    merged = _git(wt, "rev-parse", "HEAD")

    # Prepare-only run: the resolution is reported, the checkout still does not move.
    _integrate_upstream_in_worktree(["git"], fork, "upstream/main", apply=False)

    out = capsys.readouterr().out
    assert "is already merged as" in out
    assert "hermes update --upstream" in out
    assert _git(fork, "rev-parse", "HEAD") == before, "prepare-only must not move the checkout"

    # The applying run lands THAT merge — not a second merge of the same upstream tip.
    _integrate_upstream_in_worktree(["git"], fork, "upstream/main", apply=True)

    assert _git(fork, "rev-parse", "HEAD") == merged
    assert (fork / "f.txt").read_text() == "base\nfork\nupstream\n"
    assert _sync_worktrees(fork) == [], "the retired worktree is removed after applying"
    assert _git(fork, "branch", "--list", "upstream-sync-*").strip() == ""
    assert "fleet restart follows" in capsys.readouterr().out


def test_follow_up_commits_on_the_branch_ride_along(diverged, capsys):
    """A resolution often needs a second commit; the run must land the branch, not only the merge commit."""
    fork = diverged
    _integrate_upstream_in_worktree(["git"], fork, "upstream/main", apply=False)
    capsys.readouterr()
    wt = Path(_sync_worktrees(fork)[0].splitlines()[0].split(" ", 1)[1])

    (wt / "f.txt").write_text("base\nfork\nupstream\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "--no-edit")
    (wt / "notes.md").write_text("resolution notes\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "document the resolution")
    ready = _git(wt, "rev-parse", "HEAD")

    _integrate_upstream_in_worktree(["git"], fork, "upstream/main", apply=True)

    assert _git(fork, "rev-parse", "HEAD") == ready
    assert (fork / "notes.md").exists(), "the branch's follow-up commit is part of the merge"
    assert subprocess.run(["git", "merge-base", "--is-ancestor", "upstream/main", "HEAD"], check=False,
                          cwd=fork).returncode == 0


def test_an_unresolved_worktree_is_reported_and_never_duplicated(diverged, capsys):
    fork = diverged

    _integrate_upstream_in_worktree(["git"], fork, "upstream/main", apply=False)
    capsys.readouterr()

    _integrate_upstream_in_worktree(["git"], fork, "upstream/main", apply=True)

    out = capsys.readouterr().out
    assert "already being merged" in out
    assert "git add -A && git commit --no-edit" in out, "the message must be the resolvable next step"
    assert len(_sync_worktrees(fork)) == 1, "a second run must not stack another worktree on the same tip"
    assert _git(fork, "status", "--porcelain") == "", "the live checkout stays untouched"


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
