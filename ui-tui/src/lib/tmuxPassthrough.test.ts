import { describe, expect, it } from 'vitest'

import { parsePassthroughOption, tmuxWrap, wrapForTmux } from './tmuxPassthrough.js'

describe('wrapForTmux', () => {
  it('wraps one APC in a DCS passthrough with the payload ESCs doubled', () => {
    // Exactly the shape tmux forwards: `ESC P tmux;` + doubled payload + `ESC \`.
    expect(wrapForTmux('\x1b_Gi=1;AAAA\x1b\\')).toBe('\x1bPtmux;\x1b\x1b_Gi=1;AAAA\x1b\x1b\\\x1b\\')
  })

  it('doubles every payload ESC, so a multi-chunk transmit unwraps back to the same bytes', () => {
    // One passthrough around the whole transmit: tmux forwards the payload as-is, and the emulator
    // parses the undoubled APCs (with their `m=1` continuations) exactly as it would unwrapped.
    const seq = '\x1b_Gm=1;a\x1b\\\x1b_Gm=0;b\x1b\\'
    const wrapped = wrapForTmux(seq)
    const undoubled = wrapped.slice('\x1bPtmux;'.length, -'\x1b\\'.length).split('\x1b\x1b').join('\x1b')

    expect(wrapped.split('\x1bPtmux;')).toHaveLength(2)
    expect(undoubled).toBe(seq)
  })
})

describe('parsePassthroughOption', () => {
  it('reads the shapes tmux actually answers, including the inherited marker', () => {
    // `-A` output verbatim from tmux 3.8: `*` marks a value inherited from the global set.
    expect(parsePassthroughOption('allow-passthrough* on\n')).toBe(true)
    expect(parsePassthroughOption('allow-passthrough on\n')).toBe(true)
    expect(parsePassthroughOption('allow-passthrough* off\n')).toBe(false)
    expect(parsePassthroughOption('allow-passthrough off\n')).toBe(false)
    // No inherited value: nothing to trust, so the caller must fall back to half-blocks.
    expect(parsePassthroughOption('')).toBe(false)
  })
})

describe('tmuxWrap', () => {
  const seq = '\x1b_Gi=1;AAAA\x1b\\'

  it('wraps only under tmux', () => {
    expect(tmuxWrap(seq, {})).toBe(seq)
    expect(tmuxWrap(seq, { TMUX: '/tmp/tmux-1000/default,1,0' })).toBe(wrapForTmux(seq))
  })

  it('leaves an empty sequence alone even under tmux', () => {
    expect(tmuxWrap('', { TMUX: '/tmp/tmux-1000/default,1,0' })).toBe('')
  })
})
