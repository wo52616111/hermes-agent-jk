# hermes-agent-patched

A shareable Hermes Agent checkout with a small, maintainable local patch set.
The repository remains a complete Hermes Agent tree; this is not a TUI plugin.

## What is patched

The current patch adds provider account-capacity information to the TUI:

- context usage is moved to a dedicated capacity row;
- provider windows use `5h` and `7d` labels;
- reset countdowns are shown when the provider supplies reset timestamps;
- OpenAI Codex and Anthropic usage refreshes run after completed turns;
- failed refreshes retain the last successful snapshot;
- provider changes clear stale quota data.

## Install

Requirements: Python 3.11–3.13, `uv`, Node.js, and npm.

```bash
./scripts/hermes-local-install
```

This creates/updates the repository environment and builds the TUI bundle.
No files under `~/.hermes` are required for the application itself.

## Run

```bash
./scripts/hermes-local-run --tui
```

To make this checkout available as a short command, create a user-local
symlink:

```bash
ln -sfn "$PWD/scripts/hermes-local-run" "$HOME/.local/bin/hermes-patched"
hermes-patched --tui
```

The project does not overwrite an existing global `hermes` launcher.

## Update from Hermes upstream

Configure the upstream remote once:

```bash
git remote add upstream git@github.com:NousResearch/hermes-agent.git
```

Then update and rebuild with:

```bash
./scripts/hermes-local-update
```

The script refuses to update a dirty checkout or one with a TUI process running.
It then performs:

```text
git fetch upstream main
 git rebase upstream/main
uv sync --extra dev
npm ci --include=dev
build the TUI
```

If an upstream change conflicts with the local work, rebase stops normally so the
conflict can be resolved and tested; the script never silently drops the patch.

## Sharing across computers

Share this repository through your own GitHub fork or another Git remote. On a
new computer:

```bash
git clone <your-repository-url> hermes-agent-patched
cd hermes-agent-patched
./scripts/hermes-local-install
./scripts/hermes-local-run --tui
```

The repository contains the source changes and the update/install workflow. It
does not contain credentials, `~/.hermes` state, virtualenvs, `node_modules`, or
machine-specific absolute paths.

## Relationship to upstream

This project is maintained as a downstream branch of
`NousResearch/hermes-agent`:

```text
upstream/main
    ↓ rebase
local/hermes-capacity-statusbar
```

If this feature is useful broadly, the commits can later be submitted upstream.
Until then, `scripts/hermes-local-update` is the supported downstream update
path.
