import { describe, expect, it } from 'vitest'

import { detectTerminalGraphics } from './terminalGraphics.js'

const forwardsNothing = () => false
const forwardsWrapped = () => true

describe('detectTerminalGraphics', () => {
  it('picks kitty placeholders only for terminals that paint them', () => {
    expect(detectTerminalGraphics({ KITTY_WINDOW_ID: '1', TERM: 'xterm-kitty' }, forwardsNothing)).toBe('kitty')
    expect(detectTerminalGraphics({ TERM: 'xterm-ghostty' }, forwardsNothing)).toBe('kitty')
    expect(detectTerminalGraphics({ TERM_PROGRAM: 'ghostty' }, forwardsNothing)).toBe('kitty')
    expect(detectTerminalGraphics({ TERM: 'xterm-256color', TERM_PROGRAM: 'iTerm.app' }, forwardsNothing)).toBe('unicode')
    expect(detectTerminalGraphics({ TERM_PROGRAM: 'WezTerm', TERM: 'xterm-kitty' }, forwardsNothing)).toBe('unicode')
  })

  it('falls back to half-blocks where the kitty transmit is swallowed or leaked env lies', () => {
    const kitty = { KITTY_WINDOW_ID: '1', TERM: 'xterm-kitty' }

    expect(detectTerminalGraphics({ ...kitty, TMUX: '/tmp/tmux-1000/default,1,0' }, forwardsNothing)).toBe('unicode')
    expect(detectTerminalGraphics({ ...kitty, STY: '1234.pts-0.host' }, forwardsWrapped)).toBe('unicode')
    expect(detectTerminalGraphics({ ...kitty, TERM_PROGRAM: 'vscode' }, forwardsWrapped)).toBe('unicode')
    expect(detectTerminalGraphics({ ...kitty, HERMES_PTY_HOST: 'dashboard' }, forwardsWrapped)).toBe('unicode')
  })

  it('takes kitty inside tmux only when the pane allows passthrough', () => {
    // Under tmux TERM/TERM_PROGRAM name the multiplexer; the inherited KITTY_WINDOW_ID is what
    // still says the real terminal paints placeholders.
    const inTmux = { KITTY_WINDOW_ID: '1', TERM: 'tmux-256color', TERM_PROGRAM: 'tmux', TMUX: '/tmp/tmux-1000/default,1,0' }

    expect(detectTerminalGraphics(inTmux, forwardsWrapped)).toBe('kitty')
    expect(detectTerminalGraphics(inTmux, forwardsNothing)).toBe('unicode')
    // A terminal that cannot paint placeholders stays on half-blocks even with passthrough on.
    expect(detectTerminalGraphics({ ...inTmux, KITTY_WINDOW_ID: undefined }, forwardsWrapped)).toBe('unicode')
  })
})
