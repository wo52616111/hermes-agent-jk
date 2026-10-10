import { describe, expect, it } from 'vitest'

import {
  CHALK_USES_RICH_EIGHT_BIT_DOWNGRADE,
  clampChalkLevelForTmux,
  richEightBitColorNumber,
  shouldUseRichEightBitDowngradeForLegacyAppleTerminal,
  tmuxClientSupportsRgb
} from './colorize.js'

describe('shouldUseRichEightBitDowngradeForLegacyAppleTerminal', () => {
  it('memoizes the current process decision for render hot paths', () => {
    expect(typeof CHALK_USES_RICH_EIGHT_BIT_DOWNGRADE).toBe('boolean')
  })

  it('uses Rich-compatible 256-color downgrade on legacy Apple Terminal', () => {
    expect(
      shouldUseRichEightBitDowngradeForLegacyAppleTerminal({ TERM_PROGRAM: 'Apple_Terminal' } as NodeJS.ProcessEnv, 2)
    ).toBe(true)
  })

  it('normalizes Apple Terminal names before matching', () => {
    expect(
      shouldUseRichEightBitDowngradeForLegacyAppleTerminal({ TERM_PROGRAM: ' Apple_Terminal ' } as NodeJS.ProcessEnv, 2)
    ).toBe(true)
  })

  it('does not rewrite when Apple Terminal advertises truecolor', () => {
    expect(
      shouldUseRichEightBitDowngradeForLegacyAppleTerminal(
        { COLORTERM: 'truecolor', TERM_PROGRAM: 'Apple_Terminal' } as NodeJS.ProcessEnv,
        3
      )
    ).toBe(false)
  })

  it('does not override explicit color environment choices', () => {
    expect(
      shouldUseRichEightBitDowngradeForLegacyAppleTerminal(
        { FORCE_COLOR: '2', TERM_PROGRAM: 'Apple_Terminal' } as NodeJS.ProcessEnv,
        2
      )
    ).toBe(false)
    expect(
      shouldUseRichEightBitDowngradeForLegacyAppleTerminal(
        { HERMES_TUI_TRUECOLOR: '1', TERM_PROGRAM: 'Apple_Terminal' } as NodeJS.ProcessEnv,
        3
      )
    ).toBe(false)
  })
})

describe('richEightBitColorNumber', () => {
  it('matches Rich downgrade output for default Hermes skin colors', () => {
    expect(richEightBitColorNumber(0xff, 0xd7, 0x00)).toBe(220)
    expect(richEightBitColorNumber(0xff, 0xbf, 0x00)).toBe(214)
    expect(richEightBitColorNumber(0xcd, 0x7f, 0x32)).toBe(173)
    expect(richEightBitColorNumber(0xb8, 0x86, 0x0b)).toBe(136)
    expect(richEightBitColorNumber(0xff, 0xf8, 0xdc)).toBe(230)
  })
})

describe('tmuxClientSupportsRgb', () => {
  it('reports true when the client feature string includes RGB', () => {
    const exec = () => 'bpaste,ccolour,clipboard,cstyle,focus,RGB,title'

    expect(tmuxClientSupportsRgb({} as NodeJS.ProcessEnv, exec)).toBe(true)
  })

  it('reports true when the client feature string includes Tc', () => {
    const exec = () => 'bpaste,Tc,title'

    expect(tmuxClientSupportsRgb({} as NodeJS.ProcessEnv, exec)).toBe(true)
  })

  it('reports false when neither RGB nor Tc is present', () => {
    const exec = () => 'bpaste,ccolour,clipboard,cstyle,focus,title'

    expect(tmuxClientSupportsRgb({} as NodeJS.ProcessEnv, exec)).toBe(false)
  })

  it('reports unknown (null), not false, when the tmux query throws', () => {
    // A timeout on a loaded machine is "no answer", not "no RGB support".
    const exec = () => {
      throw new Error('tmux: no server running')
    }

    expect(tmuxClientSupportsRgb({} as NodeJS.ProcessEnv, exec)).toBeNull()
  })

  it('gives the query a budget that survives a loaded machine (round-trips measured at 125-270ms)', () => {
    let timeout = 0

    const exec = (_cmd: string, _args: readonly string[], opts: { timeout?: number }) => {
      timeout = opts.timeout ?? 0

      return 'RGB'
    }

    tmuxClientSupportsRgb({} as NodeJS.ProcessEnv, exec as never)

    expect(timeout).toBeGreaterThanOrEqual(1000)
  })
})

describe('clampChalkLevelForTmux', () => {
  const rgbExec = () => 'RGB'
  const noRgbExec = () => 'bpaste,title'

  it('does not clamp outside tmux', () => {
    expect(clampChalkLevelForTmux({} as NodeJS.ProcessEnv, 3)).toBe(false)
  })

  it('does not clamp when chalk is already at 256-color or below', () => {
    expect(clampChalkLevelForTmux({ TMUX: '/tmp/tmux-1' } as NodeJS.ProcessEnv, 2)).toBe(false)
  })

  it('does not clamp inside tmux when the client negotiated true RGB support (the ,xterm*:Tc case)', () => {
    // Regression: a bare `$TMUX` check used to clamp truecolor to 256-color
    // unconditionally, even for clients (e.g. kitty via
    // `terminal-overrides ,xterm*:Tc`) that tmux itself confirms support RGB
    // passthrough — visibly shifting authored hex colors (#7a5ccc rendered
    // as #8787d7 in the 6×6×6 cube).
    const env = { TMUX: '/tmp/tmux-1' } as NodeJS.ProcessEnv

    expect(clampChalkLevelForTmux(env, 3, rgbExec)).toBe(false)
  })

  it('clamps inside tmux when the client does not negotiate RGB support', () => {
    const env = { TMUX: '/tmp/tmux-1' } as NodeJS.ProcessEnv

    expect(clampChalkLevelForTmux(env, 3, noRgbExec)).toBe(true)
  })

  it('HERMES_TUI_TRUECOLOR=1 forces truecolor even without querying tmux', () => {
    const env = { HERMES_TUI_TRUECOLOR: '1', TMUX: '/tmp/tmux-1' } as NodeJS.ProcessEnv

    expect(clampChalkLevelForTmux(env, 3, noRgbExec)).toBe(false)
  })

  it('HERMES_TUI_TRUECOLOR=0 forces the clamp even when tmux reports RGB support', () => {
    const env = { HERMES_TUI_TRUECOLOR: '0', TMUX: '/tmp/tmux-1' } as NodeJS.ProcessEnv

    expect(clampChalkLevelForTmux(env, 3, rgbExec)).toBe(true)
  })

  describe('when the tmux query gives no answer', () => {
    const failExec = () => {
      throw new Error('ETIMEDOUT')
    }

    it('trusts COLORTERM=truecolor instead of quantising to 256 colours', () => {
      const env = { COLORTERM: 'truecolor', TMUX: '/tmp/tmux-1' } as NodeJS.ProcessEnv

      expect(clampChalkLevelForTmux(env, 3, failExec as never)).toBe(false)
    })

    it('trusts COLORTERM=24bit', () => {
      const env = { COLORTERM: '24bit', TMUX: '/tmp/tmux-1' } as NodeJS.ProcessEnv

      expect(clampChalkLevelForTmux(env, 3, failExec as never)).toBe(false)
    })

    it('keeps the conservative clamp when nothing else advertises truecolor', () => {
      const env = { TMUX: '/tmp/tmux-1' } as NodeJS.ProcessEnv

      expect(clampChalkLevelForTmux(env, 3, failExec as never)).toBe(true)
    })
  })

  it('still clamps when tmux answers that the client has no RGB, even if COLORTERM says truecolor', () => {
    // A definite "no" is tmux's own verdict about what it will pass through.
    const env = { COLORTERM: 'truecolor', TMUX: '/tmp/tmux-1' } as NodeJS.ProcessEnv

    expect(clampChalkLevelForTmux(env, 3, noRgbExec)).toBe(true)
  })
})
