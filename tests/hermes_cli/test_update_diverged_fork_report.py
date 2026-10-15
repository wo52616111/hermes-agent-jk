"""A fork with its own commits must never be reported as checked against upstream.

``_sync_with_upstream_if_needed`` used to print one skip line and return True ("verified"), so
``hermes update`` said "Already up to date!" for a fork that had drifted thousands of commits
behind. The fork's own commits make a fast-forward impossible, so the sync is skipped; the
skip must be reported (how far behind, conflict preview) and must return False.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli.update_cmd_git import _sync_with_upstream_if_needed


def _env(home: Path) -> dict:
    return {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(home),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          encoding="utf-8", env=_env(cwd)).stdout.strip()


def _commit(repo: Path, name: str, text: str, msg: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def diverged(tmp_path, monkeypatch):
    """A clone whose origin/main has its own commit while upstream/main moved on separately."""
    for name, value in _env(tmp_path).items():
        if name.startswith("GIT_") or name == "HOME":
            monkeypatch.setenv(name, value)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    base = _commit(upstream, "base.txt", "base\n", "base")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(upstream), str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "remote", "add", "upstream", str(upstream))
    _commit(clone, "fork.txt", "mine\n", "fork-only commit")
    _git(clone, "push", "-q", "origin", "main")
    _commit(upstream, "up.txt", "theirs\n", "upstream moves on")
    _commit(upstream, "up2.txt", "theirs2\n", "upstream moves on again")
    return clone, upstream, base


def test_a_diverged_fork_is_reported_behind_and_not_called_verified(diverged, capsys):
    clone, _upstream, _base = diverged
    head_before = _git(clone, "rev-parse", "HEAD")

    result = _sync_with_upstream_if_needed(["git"], clone, assume_yes=True)

    out = capsys.readouterr().out
    assert result is False, "a skipped upstream sync must not be reported as verified"
    assert "2 commit(s) BEHIND upstream" in out, out
    assert "1 commit(s) of its own" in out, out
    assert "official repo was NOT checked" in out, out
    assert "git merge upstream/main" in out and "git rebase upstream/main" in out, out
    assert _git(clone, "rev-parse", "HEAD") == head_before, "the non-interactive run must not move HEAD"


def test_a_conflict_free_trial_merge_is_announced_and_can_be_accepted(diverged, capsys):
    clone, _upstream, _base = diverged
    answers = []

    def say_yes(prompt, default):
        answers.append(prompt)
        return "y"

    assert _sync_with_upstream_if_needed(["git"], clone, input_fn=say_yes) is False
    out = capsys.readouterr().out
    assert "conflict-free" in out, out
    assert answers and "Merge upstream/main" in answers[0]
    assert (clone / "up.txt").exists() and (clone / "fork.txt").exists(), "both histories must be in the tree"
    assert "Merged upstream/main" in out, out


def test_declining_the_offer_leaves_the_tree_untouched(diverged, capsys):
    clone, _upstream, _base = diverged
    head_before = _git(clone, "rev-parse", "HEAD")

    assert _sync_with_upstream_if_needed(["git"], clone, input_fn=lambda p, d: "n") is False

    assert _git(clone, "rev-parse", "HEAD") == head_before
    assert not (clone / "up.txt").exists()


def test_conflicts_are_counted_and_never_offered_as_a_one_step_merge(tmp_path, monkeypatch, capsys):
    for name, value in _env(tmp_path).items():
        if name.startswith("GIT_") or name == "HOME":
            monkeypatch.setenv(name, value)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, "same.txt", "base\n", "base")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(upstream), str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "remote", "add", "upstream", str(upstream))
    _commit(clone, "same.txt", "fork edit\n", "fork edits the file")
    _git(clone, "push", "-q", "origin", "main")
    _commit(upstream, "same.txt", "upstream edit\n", "upstream edits the same line")
    head_before = _git(clone, "rev-parse", "HEAD")
    asked = []

    assert _sync_with_upstream_if_needed(["git"], clone, input_fn=lambda p, d: asked.append(p) or "y") is False

    out = capsys.readouterr().out
    assert "conflicts in 1 file(s)" in out, out
    assert asked == [], "a conflicting merge must not be offered as a one-step action"
    assert _git(clone, "rev-parse", "HEAD") == head_before


def test_a_fork_with_nothing_new_upstream_does_not_claim_to_be_behind(tmp_path, monkeypatch, capsys):
    for name, value in _env(tmp_path).items():
        if name.startswith("GIT_") or name == "HOME":
            monkeypatch.setenv(name, value)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, "a.txt", "a\n", "a")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(upstream), str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "remote", "add", "upstream", str(upstream))
    _commit(clone, "mine.txt", "m\n", "fork only")
    _git(clone, "push", "-q", "origin", "main")

    # Upstream has nothing the fork lacks: the fork IS ahead of upstream, which verifies as current.
    assert _sync_with_upstream_if_needed(["git"], clone, assume_yes=True) is True
    assert "BEHIND" not in capsys.readouterr().out
