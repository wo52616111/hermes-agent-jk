import { atom, computed } from 'nanostores'

import { MOUSE_TRACKING } from '../config/env.js'
import { ZERO } from '../domain/usage.js'
import { type BootSkin, bootSkin, bootTheme } from '../lib/themeBoot.js'
import { DEFAULT_THEME, fromSkin, skinIsLight, type Theme } from '../theme.js'

import { DEFAULT_INDICATOR_STYLE, type UiState } from './interfaces.js'

export interface ThemeSkinPayload {
  banner_hero?: string
  banner_logo?: string
  branding?: Parameters<typeof fromSkin>[1]
  colors?: Parameters<typeof fromSkin>[0]
  dark_colors?: Parameters<typeof fromSkin>[0]
  help_header?: string
  light_colors?: Parameters<typeof fromSkin>[0]
  tool_prefix?: string
}

// Both the launcher seed and gateway-ready payload enter through this resolver.
// Keeping the conversion here makes the first and settled banner share artwork,
// spacing, and every palette decision.
export function themeForSkinPayload(skin: ThemeSkinPayload | null): Theme | null {
  if (!skin) {
    return null
  }

  const base = skin.colors ?? {}
  const paired = skinIsLight(base) ? skin.light_colors : skin.dark_colors
  const colors = paired && Object.keys(paired).length ? { ...base, ...paired } : base

  return fromSkin(
    colors,
    skin.branding ?? {},
    skin.banner_logo ?? '',
    skin.banner_hero ?? '',
    skin.tool_prefix ?? '',
    skin.help_header ?? ''
  )
}

export const themeForBootSkin = (skin: BootSkin | null): Theme | null => themeForSkinPayload(skin)

export const startupTheme = themeForBootSkin(bootSkin) ?? bootTheme ?? DEFAULT_THEME

const buildUiState = (): UiState => ({
  battery: false,
  batteryStatus: null,
  bgTasks: new Set(),
  busy: false,
  busyInputMode: 'queue',
  compact: false,
  destructiveSlashConfirm: true,
  detailsMode: 'collapsed',
  detailsModeCommandOverride: false,
  focusView: false,
  indicatorStyle: DEFAULT_INDICATOR_STYLE,
  info: null,
  liveSessionCount: 0,
  inlineDiffs: true,
  mouseTracking: MOUSE_TRACKING,
  notice: null,
  pasteCollapseLines: 5,
  pasteCollapseChars: 2000,
  sections: {},
  sessionTitle: '',
  showReasoning: false,
  sid: null,
  status: 'summoning hermes…',
  statusBar: 'top',
  statusBarFields: null,
  streaming: true,
  timestamps: false,
  // The launcher-provided skin is authoritative on a cold boot; the persisted
  // theme remains the direct-Node fallback before gateway.ready.
  theme: startupTheme,
  usage: ZERO
})

export const $uiState = atom<UiState>(buildUiState())

export const $uiTheme = computed($uiState, state => state.theme)
export const $uiSessionId = computed($uiState, state => state.sid)

export const getUiState = () => $uiState.get()

export const patchUiState = (next: Partial<UiState> | ((state: UiState) => UiState)) =>
  $uiState.set(typeof next === 'function' ? next($uiState.get()) : { ...$uiState.get(), ...next })

export const resetUiState = () => $uiState.set(buildUiState())
