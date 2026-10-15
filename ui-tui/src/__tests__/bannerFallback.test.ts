import { describe, expect, it } from 'vitest'

import { caduceus, logo } from '../banner.js'
import { DEFAULT_THEME } from '../theme.js'

describe('fallback startup logo', () => {
  it('uses the downstream violet gradient instead of semantic gold colors', () => {
    const colors = logo(DEFAULT_THEME.color).map(([color]) => color)

    expect(colors).toEqual(['#ecf0c1', '#ecf0c1', '#b3a1e6', '#b3a1e6', '#7a5ccc', '#7a5ccc'])
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
