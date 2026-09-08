import { describe, expect, it } from 'vitest'

import { stringWidth } from './stringWidth.js'

describe('stringWidth', () => {
  it('treats VS15 (text-presentation selector) as narrow, not emoji width-2', () => {
    // Regression: a skin's prompt_symbol of "▶︎" (U+25B6 BLACK RIGHT-POINTING
    // TRIANGLE + U+FE0E VS15) was counted as width 2 because emoji-regex
    // still matches the base character even with an explicit text-selector
    // present. VS15 requests the narrow text glyph — the terminal renders
    // it as ONE column — so the miscounted width 2 desynced Ink's composer
    // layout from the terminal by one column, leaving a stray uneditable
    // cell next to the prompt glyph.
    expect(stringWidth('\u25b6\ufe0e')).toBe(1)
  })

  it('still counts a bare ambiguous-width symbol as narrow (no selector)', () => {
    expect(stringWidth('\u25b6')).toBe(1)
  })

  it('still counts VS16 (emoji-presentation selector) as width 2', () => {
    expect(stringWidth('\u2764\ufe0f')).toBe(2)
  })

  it('still counts an incomplete keycap (digit + VS16, no U+20E3) as width 1', () => {
    expect(stringWidth('7\ufe0f')).toBe(1)
  })

  it('counts a true single-codepoint emoji as width 2', () => {
    expect(stringWidth('\u{1f600}')).toBe(2)
  })

  it('counts a regional-indicator flag pair as width 2 and a lone one as width 1', () => {
    expect(stringWidth('\u{1f1fa}\u{1f1f8}')).toBe(2)
    expect(stringWidth('\u{1f1fa}')).toBe(1)
  })
})
