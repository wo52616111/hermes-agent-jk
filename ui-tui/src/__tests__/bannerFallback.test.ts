import { describe, expect, it } from 'vitest'

import { caduceus, logo } from '../banner.js'
import { DEFAULT_THEME } from '../theme.js'

describe('fallback startup logo', () => {
  it('uses the downstream violet gradient instead of semantic gold colors', () => {
    const colors = logo(DEFAULT_THEME.color).map(([color]) => color)

    expect(colors).toEqual([
      '#c792ea',
      '#c792ea',
      '#a855f7',
      '#a855f7',
      '#6d28d9',
      '#6d28d9'
    ])
  })

  it('uses the downstream violet gradient for the fallback caduceus', () => {
    const colors = caduceus(DEFAULT_THEME.color).map(([color]) => color)

    expect(colors).toEqual([
      '#6d4cc7',
      '#6d4cc7',
      '#8b5cf6',
      '#8b5cf6',
      '#c792ea',
      '#c792ea',
      '#aa7ae6',
      '#aa7ae6',
      '#8b5cf6',
      '#8b5cf6',
      '#6d4cc7',
      '#6d4cc7',
      '#6d4cc7',
      '#6d4cc7',
      '#6d4cc7'
    ])
  })
})
