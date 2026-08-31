import { atom, computed } from 'nanostores'

import { MOUSE_TRACKING } from '../config/env.js'
import { ZERO } from '../domain/usage.js'
import { type BootSkin, bootSkin, bootTheme } from '../lib/themeBoot.js'
import { DEFAULT_THEME, fromSkin, skinIsLight, type Theme } from '../theme.js'

import { DEFAULT_INDICATOR_STYLE, type UiState } from './interfaces.js'

export function themeForBootSkin(skin: BootSkin | null): Theme | null {
  if (!skin) {
    return null
  }

  const paired = skinIsLight(skin.colors) ? skin.light_colors : skin.dark_colors
  const colors = Object.keys(paired).length ? { ...skin.colors, ...paired } : skin.colors

  return fromSkin(colors, skin.branding, skin.banner_logo, skin.banner_hero, skin.tool_prefix, skin.help_header)
}

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
  theme: themeForBootSkin(bootSkin) ?? bootTheme ?? DEFAULT_THEME,
  usage: ZERO
})

export const $uiState = atom<UiState>(buildUiState())

export const $uiTheme = computed($uiState, state => state.theme)
export const $uiSessionId = computed($uiState, state => state.sid)

export const getUiState = () => $uiState.get()

export const patchUiState = (next: Partial<UiState> | ((state: UiState) => UiState)) =>
  $uiState.set(typeof next === 'function' ? next($uiState.get()) : { ...$uiState.get(), ...next })

export const resetUiState = () => $uiState.set(buildUiState())
