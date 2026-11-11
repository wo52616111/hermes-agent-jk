import { execFileSync } from 'node:child_process'

// tmux does not forward a bare APC image transmit: the sequence reaches the real
// terminal only inside a DCS passthrough, and only while the pane's window carries
// `allow-passthrough on`. Measured on tmux 3.8 + kitty 0.49.2 — a bare transmit
// left the emulator's image store untouched, the wrapped one added the decoded
// image, and with the option off the wrapped one was dropped as well.
const ESC = '\x1b'

/** Wrap one escape sequence for tmux's passthrough (payload ESCs doubled, one wrapper per APC chunk). */
export const wrapForTmux = (seq: string): string => `${ESC}Ptmux;${seq.split(ESC).join(ESC + ESC)}${ESC}\\`

/** Wrap only when a multiplexer is actually in the way (and there is something to wrap). */
export const tmuxWrap = (seq: string, env: NodeJS.ProcessEnv = process.env): string =>
  env.TMUX && seq ? wrapForTmux(seq) : seq

let allowed: boolean | undefined

/**
 * `tmux show -A` answers `allow-passthrough on`, `allow-passthrough* on` (value inherited from the
 * global set) or `allow-passthrough off`; an empty answer means the option reached us unresolved.
 */
export const parsePassthroughOption = (out: string): boolean => out.trim().endsWith(' on')

/**
 * Whether this pane's window lets a wrapped transmit through. Asked of tmux
 * itself (the option is window-scoped, so the pane decides) and cached. Anything
 * unanswerable answers `false`: a transmit that never arrives paints tofu, which
 * is worse than the half-block fallback.
 */
export const tmuxPassesGraphics = (env: NodeJS.ProcessEnv = process.env): boolean => {
  if (!env.TMUX) {
    return false
  }

  if (allowed !== undefined) {
    return allowed
  }

  try {
    // `-A` reports the inherited value too: without it, `show -w` prints nothing for a window that
    // has no local override, which would read as "off" for every default-configured tmux.
    const out = execFileSync('tmux', ['show', '-A', '-w', '-t', env.TMUX_PANE ?? '', 'allow-passthrough'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 2_000
    })

    allowed = parsePassthroughOption(out)
  } catch {
    allowed = false
  }

  return allowed
}
