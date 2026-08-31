# hermes-agent-jk

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

## Install for a new user

Requirements:

- macOS, Linux, WSL2, or another POSIX-like environment;
- Python 3.11–3.13;
- `uv`;
- Node.js >= 22.22;
- network access for Python and npm dependencies.

Install `uv` if necessary, then clone and install:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone <your-repository-url> hermes-agent-jk
cd hermes-agent-jk
./scripts/hermes-local-install
```

The installer creates the local Python environment, installs dependencies,
builds the TUI bundle, installs the bundled `jk-spaceduck` skin into
`$HERMES_HOME/skins`, selects it as the default skin, and rewires the current
user-writable `hermes` launcher to this checkout. It does not copy credentials
or session history from the publisher.

Authenticate using the normal Hermes flow:

```bash
hermes setup
# or use the relevant provider command, for example:
hermes auth
```

The exact provider credentials remain in the installer's own Hermes home.

## Run

```bash
hermes
```

`./scripts/hermes-local-install` makes plain `hermes` launch this checkout in TUI mode.
It reuses the current user-writable `hermes` launcher when possible; otherwise
it installs `~/.local/bin/hermes`, which must precede any other Hermes launcher
on `PATH`. Set `HERMES_LAUNCHER_PATH` to choose an explicit launcher path.

For a one-off invocation that bypasses `PATH`, run:

```bash
./scripts/hermes-local-run --tui
```

`./scripts/hermes-local-link` can be rerun after moving the checkout. The
launcher routes `hermes update` to the downstream updater.

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
git clone <your-repository-url> hermes-agent-jk
cd hermes-agent-jk
./scripts/hermes-local-install
hermes
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
hermes-agent-jk/main
```

If this feature is useful broadly, the commits can later be submitted upstream.
Until then, `scripts/hermes-local-update` is the supported downstream update
path.
