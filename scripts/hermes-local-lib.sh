# Shared by scripts/hermes-local-install and scripts/hermes-local-update.
# Source it; do not execute. Expects ROOT (checkout) and HERMES_HOME_DIR to be
# set, and `set -euo pipefail` from the caller.
#
# Node policy: package.json requires a recent Node (engines.node) and .npmrc
# turns engine-strict on, so an older system Node (24 from mise/apt, say) is
# refused by `npm ci`. Those files are upstream-owned -- loosening them would
# conflict on every upstream sync and let the TUI build on a Node it was never
# tested with. Instead we select a Node that satisfies engines.node: PATH
# first, then Hermes' own tool store (`hermes pm env node`), and if neither has
# one, install the pinned Node with `hermes pm install node` (sha256-verified).

hjk_die() { printf '%s: %s\n' "${HJK_PROG:-hermes-local}" "$*" >&2; exit 1; }
hjk_log() { printf '==> %s\n' "$*"; }

# Dev tooling lives in [dependency-groups], not an extra: `--extra dev` fails
# with "Extra 'dev' is not defined". `uv sync` already includes the dev group;
# naming it keeps the intent explicit.
hjk_sync_python() {
  hjk_log 'Python environment (uv sync --group dev)'
  (cd "$ROOT" && uv sync --group dev)
}

# Lowest acceptable Node major, read from package.json engines.node (">=26").
# Plain sed so it works before any Python environment exists.
hjk_node_min_major() {
  local major
  major=$(sed -n '/"engines"/,/}/s/.*"node"[[:space:]]*:[[:space:]]*">=[[:space:]]*v\{0,1\}\([0-9]\{1,\}\).*/\1/p' \
    "$ROOT/package.json" | head -n1)
  [[ -n "$major" ]] || return 1
  printf '%s\n' "$major"
}

hjk_node_ok() { # $1 = path to a node binary
  local major
  [[ -x "$1" ]] || return 1
  major=$("$1" -p 'process.versions.node.split(".")[0]' 2>/dev/null) || return 1
  [[ "$major" =~ ^[0-9]+$ ]] && (( major >= HJK_NODE_MIN_MAJOR ))
}

# Print the bin dir of a Hermes-managed Node that satisfies the floor. Ask pm
# where its store is (`pm env node` prints {"PATH": ...} for what is installed,
# or {} when node is not installed) rather than guessing a path: the store moves
# with HERMES_RUNTIME_DIR and install stamps. The older $HERMES_HOME/node layout
# is a fallback.
hjk_find_managed_node() {
  local dir
  while IFS= read -r dir; do
    [[ -n "$dir" ]] || continue
    if hjk_node_ok "$dir/node"; then printf '%s\n' "$dir"; return 0; fi
  done < <(
    "$ROOT/.venv/bin/hermes" pm env node 2>/dev/null | "$ROOT/.venv/bin/python" -c '
import json, os, sys
try:
    path = json.load(sys.stdin).get("PATH", "")
except ValueError:
    path = ""
print("\n".join(p for p in path.split(os.pathsep) if p))' || true
    printf '%s\n' "$HERMES_HOME_DIR/node/bin"
  )
  return 1
}

# Make `node`/`npm`/`npx` on PATH satisfy engines.node. Sets HJK_NODE_BIN_DIR
# to the directory prepended to PATH, or leaves it empty if PATH was fine.
hjk_select_node() {
  local sys_node
  HJK_NODE_MIN_MAJOR=$(hjk_node_min_major) \
    || hjk_die 'could not read the Node requirement from package.json'
  HJK_NODE_BIN_DIR=

  if sys_node=$(command -v node 2>/dev/null) && hjk_node_ok "$sys_node"; then
    hjk_log "Node: using $sys_node ($("$sys_node" -v))"
    return 0
  fi
  if [[ -n "${sys_node:-}" ]]; then
    hjk_log "Node: $sys_node is $("$sys_node" -v) but package.json needs >=$HJK_NODE_MIN_MAJOR"
  else
    hjk_log 'Node: none on PATH'
  fi

  HJK_NODE_BIN_DIR=$(hjk_find_managed_node || true)
  if [[ -z "$HJK_NODE_BIN_DIR" ]]; then
    hjk_log 'Node: installing the pinned Node with `hermes pm install node`'
    HERMES_HOME="$HERMES_HOME_DIR" "$ROOT/.venv/bin/hermes" pm install node \
      || hjk_die "'hermes pm install node' failed; install Node >=$HJK_NODE_MIN_MAJOR and re-run"
    HJK_NODE_BIN_DIR=$(hjk_find_managed_node || true)
  fi
  [[ -n "$HJK_NODE_BIN_DIR" ]] \
    || hjk_die "no Node >=$HJK_NODE_MIN_MAJOR available; install one and re-run"
  export PATH="$HJK_NODE_BIN_DIR:$PATH"
  hash -r
  hjk_log "Node: using $HJK_NODE_BIN_DIR/node ($(node -v))"
}

# Install only what the TUI needs and build it. Root + ui-tui + the workspaces
# it links, NOT apps/desktop: that is what pulls Electron and the native
# get-windows addon, which the TUI never uses, so they are neither downloaded
# nor compiled. That also removes any need for `--ignore-scripts`: every
# lifecycle script that does run (esbuild, node-pty, ...) is one the TUI needs,
# and a failed native build fails the install instead of passing silently.
# engine-strict stays on; hjk_select_node made the Node satisfy it.
# HERMES_LOCAL_NPM_SCOPE=all installs every workspace (needed only if you also
# build apps/desktop from this checkout).
hjk_build_tui() {
  local npm_version=${HERMES_LOCAL_NPM_VERSION:-11.17.0}
  local scope=${HERMES_LOCAL_NPM_SCOPE:-tui}
  local -a npm=(npx -y "npm@$npm_version") ci_scope=()
  case "$scope" in
    tui) ci_scope=(--include-workspace-root --workspace ui-tui --workspace ui-tui/packages/hermes-ink --workspace apps/shared) ;;
    all) ;; # every workspace, incl. apps/desktop (Electron + native addons)
    *) hjk_die "HERMES_LOCAL_NPM_SCOPE must be 'tui' or 'all', got '$scope'" ;;
  esac
  cd "$ROOT"
  hjk_log "npm ci (npm@$npm_version, scope=$scope)"
  "${npm[@]}" ci --include=dev "${ci_scope[@]}"
  hjk_log 'Build TUI'
  "${npm[@]}" run build:ink --workspace ui-tui
  "${npm[@]}" run build --workspace ui-tui
}

# A finished build must have produced the bundle the launcher runs.
hjk_assert_tui_built() {
  [[ -f "$ROOT/ui-tui/dist/entry.js" ]] \
    || hjk_die 'TUI build finished but ui-tui/dist/entry.js is missing'
}

# `hermes --tui` resolves its Node through pm (the tool store) itself, so a Node
# fetched outside PATH needs no PATH change. Say so, and give the override.
hjk_node_runtime_note() {
  [[ -n "${HJK_NODE_BIN_DIR:-}" ]] || return 0
  printf 'Note: built with Node from %s (PATH has none >=%s).\n' "$HJK_NODE_BIN_DIR" "$HJK_NODE_MIN_MAJOR"
  printf '      `hermes --tui` finds it through Hermes'"'"' tool store; export HERMES_NODE=<path>/node to override.\n'
}
