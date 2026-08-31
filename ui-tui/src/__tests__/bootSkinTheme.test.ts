import { describe, expect, it } from 'vitest'

import { themeForBootSkin } from '../app/uiStore.js'

describe('themeForBootSkin', () => {
  it('uses a launcher-provided custom banner before gateway.ready', () => {
    const theme = themeForBootSkin({
      banner_hero: '[#7a5ccc]hero[/]',
      banner_logo: '[#d8c8ff]logo[/]',
      branding: { agent_name: 'Hermes Agent' },
      colors: { background: '#000000', ui_accent: '#b3a1e6', ui_primary: '#d8c8ff' },
      dark_colors: {},
      light_colors: {},
      tool_prefix: '┊'
    })

    expect(theme?.bannerLogo).toBe('[#d8c8ff]logo[/]')
    expect(theme?.bannerHero).toBe('[#7a5ccc]hero[/]')
    expect(theme?.color.primary).toBe('#d8c8ff')
  })
})
