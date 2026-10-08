import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import {
  formatResetRemaining,
  layoutQuotaGroups,
  type QuotaSegment,
  quotaStaleMarker,
  StatusRule
} from '../components/appChrome.js'
import type { AccountUsageGroup } from '../gatewayTypes.js'
import { DEFAULT_THEME } from '../theme.js'

type ReactNodeLike = React.ReactNode

const textContent = (node: ReactNodeLike): string => {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return ''
  }

  if (typeof node === 'string' || typeof node === 'number') {
    return String(node)
  }

  if (Array.isArray(node)) {
    return node.map(textContent).join('')
  }

  if (React.isValidElement(node)) {
    return textContent(node.props.children)
  }

  return ''
}

const findClickableWithText = (node: ReactNodeLike, needle: string): React.ReactElement | null => {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return null
  }

  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findClickableWithText(child, needle)

      if (found) {
        return found
      }
    }

    return null
  }

  if (!React.isValidElement(node)) {
    return null
  }

  if (typeof node.props.onClick === 'function' && textContent(node).includes(needle)) {
    return node
  }

  return findClickableWithText(node.props.children, needle)
}

// Find the innermost element whose own (direct) text content includes the
// needle. Used to assert the colour the notice text is rendered with.
const findElementWithText = (node: ReactNodeLike, needle: string): React.ReactElement | null => {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return null
  }

  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElementWithText(child, needle)

      if (found) {
        return found
      }
    }

    return null
  }

  if (!React.isValidElement(node)) {
    return null
  }

  // Prefer the deepest matching element so we get the leaf <Text> that
  // actually carries the colour, not an ancestor Box.
  const deeper = findElementWithText(node.props.children, needle)

  if (deeper) {
    return deeper
  }

  return textContent(node).includes(needle) ? node : null
}

const baseProps = {
  bgCount: 0,
  busy: false,
  cols: 100,
  cwdLabel: '~/repo',
  liveSessionCount: 0,
  model: 'opus-4.8',
  sessionStartedAt: null,
  status: 'ready',
  statusColor: DEFAULT_THEME.color.ok,
  t: DEFAULT_THEME,
  turnStartedAt: null,
  usage: {
    calls: 1,
    input: 1,
    output: 1,
    context_max: 200_000,
    context_percent: 25,
    context_used: 50_000,
    total: 50_000
  },
  voiceLabel: ''
}

describe('StatusRule model label', () => {
  it('shows a clamped effort as what the route sends, never as a distinct level (#61634)', () => {
    const clamped = textContent(
      StatusRule({ ...baseProps, modelReasoningEffort: 'ultra', modelReasoningEffortWire: 'max' })
    )

    expect(clamped).toContain('ultra→max')
    // Verbatim (or not-yet-stamped) wire levels make no claim.
    expect(
      textContent(StatusRule({ ...baseProps, modelReasoningEffort: 'high', modelReasoningEffortWire: 'high' }))
    ).toContain('opus 4.8 high')
    expect(textContent(StatusRule({ ...baseProps, modelReasoningEffort: 'ultra' }))).toContain('opus 4.8 ultra')
  })
})

describe('StatusRule capacity row', () => {
  it('moves context and provider quota onto a dedicated second row', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-28T10:00:00Z'))

    const element = StatusRule({
      ...baseProps,
      usage: {
        ...baseProps.usage,
        account_usage: {
          provider: 'openai-codex',
          fetched_at: '2026-08-28T10:00:00Z',
          windows: [
            { period: '5h', used_percent: 34, reset_at: '2026-08-28T12:15:00Z' },
            { period: '7d', used_percent: 62, reset_at: '2026-09-02T08:00:00Z' }
          ]
        }
      }
    })

    const rows = React.Children.toArray(element.props.children)

    expect(rows).toHaveLength(2)
    expect(textContent(rows[0])).not.toContain('50k/200k')
    const capacity = rows[1] as React.ReactElement<any>
    expect(capacity.type).toBeDefined()
    expect(capacity.props.ctxLabel).toBe('50k/200k')
    expect(capacity.props.quotaWindows.map((window: { period: string }) => window.period)).toEqual(['5h', '7d'])
    expect(formatResetRemaining('2026-08-28T12:15:00Z', Date.parse('2026-08-28T10:00:00Z'))).toBe('2h 15m')
    expect(formatResetRemaining('2026-09-02T08:00:00Z', Date.parse('2026-08-28T10:00:00Z'))).toBe('4d 22h')

    vi.useRealTimers()
  })

  it('keeps context on the capacity row before account quota is available', () => {
    const element = StatusRule({ ...baseProps })
    const rows = React.Children.toArray(element.props.children)

    expect(rows).toHaveLength(2)
    expect(textContent(rows[0])).not.toContain('50k/200k')
    expect((rows[1] as React.ReactElement<any>).props.ctxLabel).toBe('50k/200k')
  })
})

const NOW = Date.parse('2026-08-28T10:00:00Z')

const group = (overrides: Partial<AccountUsageGroup> & Pick<AccountUsageGroup, 'provider'>): AccountUsageGroup => ({
  age_s: 30,
  backoff_until: null,
  error: null,
  fetched_at: '2026-08-28T09:59:30Z',
  windows: [],
  ...overrides
})

const ALL_GROUPS: AccountUsageGroup[] = [
  group({
    provider: 'anthropic',
    windows: [
      { period: '5h', used_percent: 17, reset_at: '2026-08-28T13:00:00Z' },
      { period: '7d', used_percent: 66, reset_at: '2026-08-31T10:00:00Z' },
      { period: 'opus 7d', used_percent: 91, reset_at: null },
      { period: 'sonnet 7d', used_percent: 12, reset_at: null }
    ]
  }),
  group({
    provider: 'openai-codex',
    windows: [
      { period: '5h', used_percent: 5, reset_at: '2026-08-28T12:00:00Z' },
      { period: '7d', used_percent: 7, reset_at: '2026-09-01T10:00:00Z' }
    ]
  }),
  group({
    provider: 'opencode-go',
    windows: [
      { period: '5h', used_percent: 0, reset_at: null },
      { period: 'monthly', used_percent: 36, reset_at: '2026-09-10T10:00:00Z' }
    ]
  })
]

const joined = (segments: QuotaSegment[]) => segments.map(s => s.text).join('')

// Render the CapacityRow element (second StatusRule child) to its flat text.
const capacityText = (row: React.ReactElement<any>) =>
  textContent((row.type as (p: unknown) => ReactNodeLike)(row.props))

describe('StatusRule multi-provider quota (account_usage_all)', () => {
  it('highlights the active provider label in accent bold', () => {
    const segs = layoutQuotaGroups(ALL_GROUPS, 9999, DEFAULT_THEME, NOW, 'openai-codex')
    const labels = segs.filter(s => /│ (A\\|codex|go)$/.test(s.text))

    expect(labels.map(s => s.text.trim())).toEqual(['│ A\\', '│ codex', '│ go'])
    expect(labels.map(s => !!s.bold)).toEqual([false, true, false])
    expect(labels[1]!.color).toBe(DEFAULT_THEME.color.accent)
    expect(labels[0]!.color).toBe(DEFAULT_THEME.color.muted)
  })

  it('keeps the active provider when narrow widths drop trailing groups', () => {
    const text = joined(layoutQuotaGroups(ALL_GROUPS, 12, DEFAULT_THEME, NOW, 'opencode-go'))

    expect(text).toBe(' │ go mo 36%')
    const two = joined(layoutQuotaGroups(ALL_GROUPS, 24, DEFAULT_THEME, NOW, 'opencode-go'))

    expect(two).toBe(' │ go 5h 0% mo 36%')
  })

  it('keeps the active provider 5h window when the other 5h windows drop', () => {
    const no5h = joined(layoutQuotaGroups(ALL_GROUPS, 9999, DEFAULT_THEME, NOW, 'openai-codex'))
    const budget = ' │ A\\ 7d 66% │ codex 5h 5% 7d 7% │ go mo 36%'.length

    expect(no5h).toContain('codex 5h 5%')
    expect(joined(layoutQuotaGroups(ALL_GROUPS, budget, DEFAULT_THEME, NOW, 'openai-codex'))).toBe(
      ' │ A\\ 7d 66% │ codex 5h 5% 7d 7% │ go mo 36%'
    )
  })

  it('highlights the active provider even when it is the only group shown', () => {
    const segs = layoutQuotaGroups(ALL_GROUPS.slice(0, 1), 9999, DEFAULT_THEME, NOW, 'anthropic')
    const label = segs.find(s => s.text === ' │ A\\')

    expect(label?.bold).toBe(true)
    expect(label?.color).toBe(DEFAULT_THEME.color.accent)
  })

  it('renders one labelled group per provider, skipping opus/sonnet sub-windows', () => {
    const text = joined(layoutQuotaGroups(ALL_GROUPS, 9999, DEFAULT_THEME, NOW))

    expect(text).toBe(' │ A\\ 5h 17% 7d 66% ↻ 3d │ codex 5h 5% 7d 7% ↻ 4d │ go 5h 0% mo 36% ↻ 13d')
    expect(text).not.toContain('opus')
    expect(text).not.toContain('91%')
  })

  it('colours each percentage with the quota thresholds', () => {
    const segs = layoutQuotaGroups(
      [
        group({
          provider: 'anthropic',
          windows: [
            { period: '5h', used_percent: 95, reset_at: null },
            { period: '7d', used_percent: 75, reset_at: null }
          ]
        }),
        group({ provider: 'openai-codex', windows: [{ period: '5h', used_percent: 10, reset_at: null }] })
      ],
      9999,
      DEFAULT_THEME,
      NOW
    )

    expect(segs.find(s => s.text === ' 5h 95%')?.color).toBe(DEFAULT_THEME.color.error)
    expect(segs.find(s => s.text === ' 7d 75%')?.color).toBe(DEFAULT_THEME.color.warn)
    expect(segs.find(s => s.text === ' 5h 10%')?.color).toBe(DEFAULT_THEME.color.statusGood)
    expect(segs.find(s => s.text === ' │ A\\')?.color).toBe(DEFAULT_THEME.color.muted)
  })

  it('degrades on narrow widths: resets first, then 5h windows, then trailing groups', () => {
    const noResets = ' │ A\\ 5h 17% 7d 66% │ codex 5h 5% 7d 7% │ go 5h 0% mo 36%'
    const no5h = ' │ A\\ 7d 66% │ codex 7d 7% │ go mo 36%'

    expect(joined(layoutQuotaGroups(ALL_GROUPS, noResets.length, DEFAULT_THEME, NOW))).toBe(noResets)
    expect(joined(layoutQuotaGroups(ALL_GROUPS, noResets.length - 1, DEFAULT_THEME, NOW))).toBe(no5h)
    expect(joined(layoutQuotaGroups(ALL_GROUPS, no5h.length - 1, DEFAULT_THEME, NOW))).toBe(' │ A\\ 7d 66% │ codex 7d 7%')
    expect(joined(layoutQuotaGroups(ALL_GROUPS, 12, DEFAULT_THEME, NOW))).toBe(' │ A\\ 7d 66%')
    expect(layoutQuotaGroups(ALL_GROUPS, 5, DEFAULT_THEME, NOW)).toEqual([])
  })

  it('puts stale age before its provider label and keeps a 429 marker after the group', () => {
    expect(quotaStaleMarker({ age_s: 60, error: null })).toBe('')
    expect(quotaStaleMarker({ age_s: 45 * 60, error: null })).toBe('◷ 45m')
    expect(quotaStaleMarker({ age_s: 2 * 3600 + 120, error: null })).toBe('◷ 2h')
    expect(quotaStaleMarker({ age_s: 300, error: { message: 'rate limited', status: 429 } })).toBe('·429')
    expect(quotaStaleMarker({ age_s: 3 * 3600, error: { message: 'rate limited', status: 429 } })).toBe('◷ 3h')
    expect(quotaStaleMarker({ age_s: 600, error: { message: 'boom', status: 500 } })).toBe('·10m')

    const segs = layoutQuotaGroups(
      [
        group({
          age_s: 120,
          error: { message: 'rate limited', status: 429 },
          provider: 'anthropic',
          windows: [{ period: '5h', used_percent: 40, reset_at: null }]
        }),
        group({
          age_s: 50 * 60,
          provider: 'openai-codex',
          windows: [{ period: '5h', used_percent: 1, reset_at: null }]
        })
      ],
      9999,
      DEFAULT_THEME,
      NOW
    )

    expect(joined(segs)).toBe(' │ A\\ 5h 40%·429 │ ◷ 50m codex 5h 1%')
    const marker = segs.find(s => s.text === '·429')!

    expect(marker.color).toBe(DEFAULT_THEME.color.muted)
    expect(marker.dim).toBe(true)
  })

  it('renders a route hint after its group in warn colour', () => {
    const segs = layoutQuotaGroups(
      [
        group({ hint: '→ sol', provider: 'anthropic', windows: [{ period: '5h', used_percent: 92, reset_at: null }] }),
        group({ provider: 'openai-codex', windows: [{ period: '5h', used_percent: 3, reset_at: null }] })
      ],
      9999,
      DEFAULT_THEME,
      NOW
    )

    expect(joined(segs)).toBe(' │ A\\ 5h 92% → sol │ codex 5h 3%')
    expect(segs.find(s => s.text === ' → sol')?.color).toBe(DEFAULT_THEME.color.warn)
  })

  it('keeps the active provider on the capacity row and moves other subscriptions below it', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(NOW))

    const element = StatusRule({
      ...baseProps,
      cols: 200,
      usage: {
        calls: 0,
        input: 0,
        output: 0,
        total: 0,
        account_usage: {
          provider: 'openai-codex',
          fetched_at: '2026-08-28T10:00:00Z',
          windows: [{ period: '5h', used_percent: 99, reset_at: null }]
        },
        account_usage_all: ALL_GROUPS,
        account_usage_active: 'anthropic'
      }
    })

    const rows = React.Children.toArray(element.props.children)
    const activeCapacity = rows[1] as React.ReactElement<any>
    const otherCapacity = rows[2] as React.ReactElement<any>

    expect(rows).toHaveLength(3)
    expect(activeCapacity.props.quotaGroups.map((group: AccountUsageGroup) => group.provider)).toEqual(['anthropic'])
    expect(activeCapacity.props.activeProvider).toBe('anthropic')
    expect(activeCapacity.props.quotaWindows).toEqual([])
    expect(capacityText(activeCapacity)).toBe('─ usage │ A\\ 5h 17% 7d 66% ↻ 3d')

    expect(otherCapacity.props.quotaGroups.map((group: AccountUsageGroup) => group.provider)).toEqual([
      'openai-codex',
      'opencode-go'
    ])
    expect(otherCapacity.props.activeProvider).toBeNull()
    expect(capacityText(otherCapacity)).toBe('─ usage │ codex 5h 5% 7d 7% ↻ 4d │ go 5h 0% mo 36% ↻ 13d')
    expect(`${capacityText(activeCapacity)}${capacityText(otherCapacity)}`).not.toContain('99%')

    vi.useRealTimers()
  })

  it('falls back to the single-provider account_usage when account_usage_all is missing or empty', () => {
    for (const all of [undefined, []]) {
      const element = StatusRule({
        ...baseProps,
        usage: {
          ...baseProps.usage,
          account_usage: {
            provider: 'openai-codex',
            fetched_at: '2026-08-28T10:00:00Z',
            windows: [{ period: '5h', used_percent: 34, reset_at: null }]
          },
          ...(all ? { account_usage_all: all } : {})
        }
      })

      const capacity = React.Children.toArray(element.props.children)[1] as React.ReactElement<any>

      expect(capacity.props.quotaGroups).toEqual([])
      expect(capacityText(capacity)).toContain('│ 5h 34%')
    }
  })
})

describe('StatusRule session title', () => {
  it('marks only estimated context occupancy at every visible width', () => {
    for (const cols of [80, 120, 200]) {
      for (const estimated of [true, false]) {
        const element = StatusRule({
          ...baseProps,
          cols,
          statusBarFields: new Set(['context_detail']),
          usage: { ...baseProps.usage, context_estimated: estimated }
        })

        // Context occupancy renders on the dedicated capacity row (second child),
        // so read that row's `ctxLabel` instead of the flattened transcript text.
        const capacity = React.Children.toArray(element.props.children)[1] as React.ReactElement<any>
        const context = String(capacity.props.ctxLabel).match(/(~?\d+(?:\.\d+)?k(?:\/\d+k| tok))/)?.at(0)

        expect(context, `context must render at ${cols} columns`).toBeTruthy()
        expect(context?.startsWith('~')).toBe(estimated)
      }
    }
  })

  it('pins the named session at the far-right edge instead of the cwd label', () => {
    const element = StatusRule({
      ...baseProps,
      sessionTitle: 'weekly-digest'
    })

    const rendered = textContent(element)
    const title = findElementWithText(element, 'weekly-digest')

    expect(rendered).toContain('weekly-digest')
    expect(rendered).not.toContain('~/repo')
    // Regression for issue #82465: a raw, full-saturation accent-hue
    // background (e.g. #FFBF00 on DARK_SEEDS) paired with statusFg (a
    // near-white tone never designed to sit on it) rendered at roughly a
    // 1.5-2:1 contrast ratio -- unreadable. No background fill at all;
    // the accent color goes on the text instead, matching the theme's
    // own convention that a raw accent hue is never used as a solid
    // fill elsewhere (fills are always softened, e.g. activeRow).
    expect(title?.props.backgroundColor).toBeUndefined()
    expect(title?.props.color).toBe(DEFAULT_THEME.color.accent)
  })
})

describe('StatusRule background-subagent indicator', () => {
  it('renders ⛓ N on a wide terminal when subagents are running', () => {
    const element = StatusRule({
      ...baseProps,
      usage: { ...baseProps.usage, active_subagents: 3 }
    })

    expect(textContent(element)).toContain('⛓ 3')
  })

  it('omits the segment when no subagents are running', () => {
    const element = StatusRule({
      ...baseProps,
      usage: { ...baseProps.usage, active_subagents: 0 }
    })

    expect(textContent(element)).not.toContain('⛓')
  })

  it('omits the segment when the field is absent', () => {
    const element = StatusRule({ ...baseProps })

    expect(textContent(element)).not.toContain('⛓')
  })

  it('spells out the auto-resume hint when idle with subagents in flight', () => {
    const element = StatusRule({
      ...baseProps,
      usage: { ...baseProps.usage, active_subagents: 1 }
    })

    expect(textContent(element)).toContain('resumes when subagent finishes')
  })

  it('pluralizes the resume hint for multiple in-flight subagents', () => {
    const element = StatusRule({
      ...baseProps,
      usage: { ...baseProps.usage, active_subagents: 3 }
    })

    expect(textContent(element)).toContain('resumes when 3 subagents finish')
  })

  it('hides the resume hint mid-turn (a busy turn owns the indicator)', () => {
    const element = StatusRule({
      ...baseProps,
      busy: true,
      turnStartedAt: Date.now(),
      usage: { ...baseProps.usage, active_subagents: 2 }
    })

    expect(textContent(element)).not.toContain('resumes when')
  })

  it('omits the resume hint when no subagents are running', () => {
    const element = StatusRule({ ...baseProps })

    expect(textContent(element)).not.toContain('resumes when')
  })

  it('drops the subagent segment before the bg segment on a narrow terminal', () => {
    // cols=44 is below the subagents breakpoint (92) but the bg breakpoint
    // (88) too — both gone. Assert the lower-priority subagent indicator is
    // not shown when space is tight even with a live count.
    const element = StatusRule({
      ...baseProps,
      cols: 44,
      bgCount: 1,
      usage: { ...baseProps.usage, active_subagents: 2 }
    })

    expect(textContent(element)).not.toContain('⛓')
  })
})

describe('StatusRule session count click target', () => {
  it('makes the live session count itself clickable', () => {
    const openSwitcher = vi.fn()

    const element = StatusRule({
      bgCount: 0,
      busy: false,
      cols: 100,
      cwdLabel: '~/repo',
      liveSessionCount: 1,
      model: 'kimi-k2.6',
      onSessionCountClick: openSwitcher,
      sessionStartedAt: null,
      status: 'ready',
      statusColor: DEFAULT_THEME.color.ok,
      t: DEFAULT_THEME,
      turnStartedAt: null,
      usage: { total: 0 },
      voiceLabel: ''
    })

    const clickableSessionCount = findClickableWithText(element, '1 session')

    expect(clickableSessionCount).not.toBeNull()
    clickableSessionCount!.props.onClick({ stopImmediatePropagation: vi.fn() })
    expect(openSwitcher).toHaveBeenCalledOnce()
  })

  it('keeps status + model and drops the low-value tail on a narrow terminal', () => {
    const element = StatusRule({
      bgCount: 0,
      busy: false,
      cols: 44,
      cwdLabel: '~/src/hermes-agent/apps/desktop (bb/tui-statusbar-responsive)',
      liveSessionCount: 3,
      model: 'opus-4.8',
      onSessionCountClick: vi.fn(),
      sessionStartedAt: Date.now() - 60_000,
      status: 'ready',
      statusColor: DEFAULT_THEME.color.ok,
      t: DEFAULT_THEME,
      turnStartedAt: null,
      usage: {
        calls: 0,
        context_max: 200_000,
        context_percent: 25,
        context_used: 50_000,
        input: 0,
        output: 0,
        total: 50_000
      },
      voiceLabel: 'voice off'
    })

    const rendered = textContent(element)

    // Must-keep essentials survive intact …
    expect(rendered).not.toContain('ready')
    expect(rendered).toContain('opus 4.8')
    // … while the low-value tail (session count) is dropped, not truncated.
    expect(rendered).not.toContain('3 sessions')
  })
})

describe('StatusRule credits notice render priority', () => {
  it('replaces the idle status with the notice text and keeps model + context', () => {
    const element = StatusRule({
      ...baseProps,
      notice: { key: 'credits.depleted', kind: 'sticky', level: 'error', text: '✕ credits exhausted' }
    })

    const rendered = textContent(element)

    // Notice replaces the status verb slot …
    expect(rendered).toContain('✕ credits exhausted')
    expect(rendered).not.toContain('ready')
    // … but model + context stay visible.
    expect(rendered).toContain('opus 4.8')
    expect((React.Children.toArray(element.props.children)[1] as React.ReactElement<any>).props.ctxLabel).toBe(
      '50k/200k'
    )
  })

  it('busy wins: the FaceTicker shows, the notice is hidden mid-turn', () => {
    const element = StatusRule({
      ...baseProps,
      busy: true,
      notice: { key: 'credits.90', kind: 'sticky', level: 'warn', text: '⚠ 90% used' },
      turnStartedAt: Date.now()
    })

    const rendered = textContent(element)

    // Notice must NOT render while busy.
    expect(rendered).not.toContain('⚠ 90% used')
    // Model still visible.
    expect(rendered).toContain('opus 4.8')
  })

  it('colours the notice by level (error → theme error, success → statusGood)', () => {
    const errEl = StatusRule({
      ...baseProps,
      notice: { key: 'credits.depleted', kind: 'sticky', level: 'error', text: '✕ exhausted' }
    })

    const errText = findElementWithText(errEl, '✕ exhausted')
    expect(errText?.props.color).toBe(DEFAULT_THEME.color.error)

    const okEl = StatusRule({
      ...baseProps,
      notice: { key: 'credits.restored', kind: 'ttl', level: 'success', text: '✓ restored', ttl_ms: 8000 }
    })

    const okText = findElementWithText(okEl, '✓ restored')
    expect(okText?.props.color).toBe(DEFAULT_THEME.color.statusGood)
  })

  it('does NOT add a glyph — the notice text is rendered verbatim', () => {
    const element = StatusRule({
      ...baseProps,
      notice: { key: 'credits.90', kind: 'sticky', level: 'warn', text: '⚠ 90% used' }
    })

    const noticeText = findElementWithText(element, '90% used')

    // The leaf carries exactly the policy text — no extra prepended glyph.
    expect(noticeText?.props.children).toBe('⚠ 90% used')
  })

  it('the notice text is the shrinkable element (flexShrink=1 + truncate-end) so a long notice ellipsizes', () => {
    const longText = '⚠ ' + 'x'.repeat(200)

    const element = StatusRule({
      ...baseProps,
      cols: 50,
      notice: { key: 'credits.90', kind: 'sticky', level: 'warn', text: longText }
    })

    // The leaf <Text> truncates rather than wrapping/clipping the pinned tail.
    const noticeText = findElementWithText(element, 'xxxxx')
    expect(noticeText?.props.wrap).toBe('truncate-end')

    // Its container box yields first (flexShrink=1) so model stays visible.
    const findShrinkBoxContaining = (node: ReactNodeLike): React.ReactElement | null => {
      if (!React.isValidElement(node)) {
        if (Array.isArray(node)) {
          for (const c of node) {
            const f = findShrinkBoxContaining(c)

            if (f) {
              return f
            }
          }
        }

        return null
      }

      if (node.props.flexShrink === 1 && textContent(node).includes('xxxxx') && node.type !== StatusRule) {
        // Prefer the closest shrink box that wraps the notice text.
        const deeper = findShrinkBoxContaining(node.props.children)

        return deeper ?? node
      }

      return findShrinkBoxContaining(node.props.children)
    }

    const shrinkBox = findShrinkBoxContaining(element)
    expect(shrinkBox).not.toBeNull()

    // Model survives on a narrow terminal because the notice yields.
    expect(textContent(element)).toContain('opus 4.8')
  })
})

describe('StatusRule battery indicator', () => {
  it('renders the battery label with a battery glyph on AC-off', () => {
    const element = StatusRule({
      ...baseProps,
      battery: { available: true, category: 'good', percent: 82, plugged: false }
    })

    expect(textContent(element)).toContain('🔋 82%')
  })

  it('uses a bolt glyph while charging', () => {
    const element = StatusRule({
      ...baseProps,
      battery: { available: true, category: 'good', percent: 82, plugged: true }
    })

    expect(textContent(element)).toContain('⚡ 82%')
  })

  it('colours the read-out by category (critical → theme statusCritical)', () => {
    const element = StatusRule({
      ...baseProps,
      battery: { available: true, category: 'critical', percent: 7, plugged: false }
    })

    const leaf = findElementWithText(element, '7%')
    expect(leaf?.props.color).toBe(DEFAULT_THEME.color.statusCritical)
  })

  it('omits the segment when battery is null', () => {
    const element = StatusRule({ ...baseProps, battery: null })

    expect(textContent(element)).not.toContain('%🔋')
    expect(textContent(element)).not.toContain('🔋')
  })

  it('omits the segment when no battery is available (desktop/server)', () => {
    const element = StatusRule({
      ...baseProps,
      battery: { available: false, category: 'dim', percent: null, plugged: null }
    })

    expect(textContent(element)).not.toContain('🔋')
  })
})

describe('StatusRule idle-since read-out', () => {
  // The IdleSince component uses hooks, so it can't be invoked outside a
  // renderer — assert on the element tree instead (same reason the duration
  // tests don't check SessionDuration's text).
  const findComponentByName = (node: ReactNodeLike, name: string): React.ReactElement | null => {
    if (node === null || node === undefined || typeof node === 'boolean') {
      return null
    }

    if (Array.isArray(node)) {
      for (const child of node) {
        const found = findComponentByName(child, name)

        if (found) {
          return found
        }
      }

      return null
    }

    if (!React.isValidElement(node)) {
      return null
    }

    if (typeof node.type === 'function' && node.type.name === name) {
      return node
    }

    return findComponentByName(node.props.children, name)
  }

  it('shows time since the last final agent response when idle', () => {
    const endedAt = Date.now() - 42_000

    const element = StatusRule({
      ...baseProps,
      lastTurnEndedAt: endedAt,
      sessionStartedAt: Date.now() - 60_000
    })

    const idle = findComponentByName(element, 'IdleSince')

    expect(idle).not.toBeNull()
    expect(idle!.props.endedAt).toBe(endedAt)
  })

  it('is hidden while a turn is busy', () => {
    const element = StatusRule({
      ...baseProps,
      busy: true,
      lastTurnEndedAt: Date.now() - 42_000,
      turnStartedAt: Date.now()
    })

    expect(findComponentByName(element, 'IdleSince')).toBeNull()
  })

  it('is hidden before the first turn completes', () => {
    const element = StatusRule({
      ...baseProps,
      lastTurnEndedAt: null,
      sessionStartedAt: Date.now() - 60_000
    })

    expect(findComponentByName(element, 'IdleSince')).toBeNull()
  })

  it('honors the display.status_bar.fields filter when idle_since is omitted', () => {
    const element = StatusRule({
      ...baseProps,
      lastTurnEndedAt: Date.now() - 42_000,
      sessionStartedAt: Date.now() - 60_000,
      statusBarFields: new Set(['model', 'context_pct'])
    })

    expect(findComponentByName(element, 'IdleSince')).toBeNull()
  })
})

describe('StatusRule busy elapsed-time tail (prompt_elapsed)', () => {
  const findComponentByName = (node: ReactNodeLike, name: string): React.ReactElement<any> | null => {
    if (node === null || node === undefined || typeof node === 'boolean') {
      return null
    }

    if (Array.isArray(node)) {
      for (const child of node) {
        const found = findComponentByName(child, name)

        if (found) {
          return found
        }
      }

      return null
    }

    if (!React.isValidElement(node)) {
      return null
    }

    if (typeof node.type === 'function' && node.type.name === name) {
      return node
    }

    return findComponentByName(node.props.children, name)
  }

  it('shows the elapsed-time tail on the FaceTicker by default', () => {
    const element = StatusRule({
      ...baseProps,
      busy: true,
      turnStartedAt: Date.now() - 5_000
    })

    const face = findComponentByName(element, 'FaceTicker')

    expect(face).not.toBeNull()
    expect(face!.props.startedAt).not.toBeNull()
  })

  it('hides the elapsed-time tail when the fields filter omits prompt_elapsed', () => {
    const element = StatusRule({
      ...baseProps,
      busy: true,
      statusBarFields: new Set(['model']),
      turnStartedAt: Date.now() - 5_000
    })

    const face = findComponentByName(element, 'FaceTicker')

    expect(face).not.toBeNull()
    expect(face!.props.startedAt).toBeNull()
  })
})

describe('StatusRule perf read-outs (cache hit / latency / tps)', () => {
  const perfUsage = {
    ...baseProps.usage,
    avg_latency_s: 3.2,
    avg_tps: 50.4,
    cache_hit_pct: 87,
    calls: 4,
    input: 1000,
    output: 500
  }

  it('renders all three segments on a wide terminal', () => {
    const element = StatusRule({ ...baseProps, cols: 160, usage: perfUsage })
    const rendered = textContent(element)

    expect(rendered).toContain('◎ 87%')
    expect(rendered).toContain('◷ 3.2s')
    expect(rendered).toContain('↑ 50 t/s')
  })

  it('self-hides when the server omits the keys', () => {
    const element = StatusRule({ ...baseProps, cols: 160 })
    const rendered = textContent(element)

    expect(rendered).not.toContain('◎')
    expect(rendered).not.toContain('◷')
    expect(rendered).not.toContain('t/s')
  })

  it('honors the display.status_bar.fields visibility filter', () => {
    const element = StatusRule({
      ...baseProps,
      cols: 160,
      statusBarFields: new Set(['model', 'context_pct', 'cache_hit']),
      usage: perfUsage
    })

    const rendered = textContent(element)

    expect(rendered).toContain('◎ 87%')
    expect(rendered).not.toContain('◷')
    expect(rendered).not.toContain('t/s')
  })

  it('hides the session title badge when the fields filter omits title', () => {
    const element = StatusRule({
      ...baseProps,
      cols: 160,
      sessionTitle: 'weekly-digest',
      statusBarFields: new Set(['model', 'context_pct'])
    })

    expect(textContent(element)).not.toContain('weekly-digest')
  })
})
