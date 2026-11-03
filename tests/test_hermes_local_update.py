"""Regression coverage for the downstream updater's source ordering."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _fake_command(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def test_downstream_updater_syncs_customized_main_before_upstream_rebase(tmp_path: Path) -> None:
    """A user must receive fork releases before replaying them on upstream."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"

    _fake_command(
        bin_dir,
        "git",
        'printf "git %s\\n" "$*" >> "$UPDATE_LOG"\n'
        'if [[ "$1 $2" == "remote get-url" || "$1 $2" == "status --porcelain" ]]; then exit 0; fi',
    )
    _fake_command(bin_dir, "pgrep", "exit 1")
    _fake_command(bin_dir, "uv", 'printf "uv %s\\n" "$*" >> "$UPDATE_LOG"')
    _fake_command(bin_dir, "npx", 'printf "npx %s\\n" "$*" >> "$UPDATE_LOG"')
    # The script insists on a Node satisfying package.json engines; fake a new one
    # so the test does not depend on the host's Node (or download one).
    _fake_command(bin_dir, "node", 'case "$1" in -v) echo v99.0.0;; *) echo 99;; esac')

    env = {
        **os.environ,
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "UPDATE_LOG": str(log),
    }
    result = subprocess.run(
        [str(ROOT / "scripts/hermes-local-update")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8").splitlines()
    assert commands.index("git fetch origin main") < commands.index("git rebase origin/main")
    assert commands.index("git rebase origin/main") < commands.index("git fetch upstream main")
    assert commands.index("git fetch upstream main") < commands.index("git rebase upstream/main")


def test_downstream_updater_syncs_dev_group_not_removed_extra(tmp_path: Path) -> None:
    """`--extra dev` fails ("Extra 'dev' is not defined"): dev is a dependency group."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    _fake_command(bin_dir, "git", 'if [[ "$1 $2" == "remote get-url" || "$1 $2" == "status --porcelain" ]]; then exit 0; fi')
    _fake_command(bin_dir, "pgrep", "exit 1")
    _fake_command(bin_dir, "uv", 'printf "uv %s\\n" "$*" >> "$UPDATE_LOG"')
    _fake_command(bin_dir, "npx", 'printf "npx %s\\n" "$*" >> "$UPDATE_LOG"')
    _fake_command(bin_dir, "node", 'case "$1" in -v) echo v99.0.0;; *) echo 99;; esac')

    env = {
        **os.environ,
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "UPDATE_LOG": str(log),
    }
    result = subprocess.run(
        [str(ROOT / "scripts/hermes-local-update")],
        cwd=ROOT, check=False, capture_output=True, env=env, text=True,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8").splitlines()
    assert "uv sync --group dev" in commands
    assert not any("--extra dev" in c for c in commands)
    # TUI-only install: apps/desktop (Electron, get-windows) is not built here.
    npm_ci = next(c for c in commands if " ci " in c)
    assert "--workspace ui-tui" in npm_ci and "apps/desktop" not in npm_ci
    assert "--ignore-scripts" not in npm_ci
