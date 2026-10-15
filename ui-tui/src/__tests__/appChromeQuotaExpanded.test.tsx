import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { expandAccountGroups, layoutQuotaGroups, type QuotaSegment, StatusRule } from '../components/appChrome.js'
import type { AccountUsageAccount, AccountUsageGroup } from '../gatewayTypes.js'
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

const joined = (segments: QuotaSegment[]) => segments.map(s => s.text).join('')

const NOW = Date.parse('2026-08-28T10:00:00Z')

const account = (
  overrides: Partial<AccountUsageAccount> & Pick<AccountUsageAccount, 'fingerprint' | 'state'>
): AccountUsageAccount => ({
  label: 'OPENCODE_GO_API_KEY',
  entry_id: null,
  metered: false,
  read_health: 'ok',
  windows: [],
  fetched_at: '2026-08-28T09:59:30Z',
  age_s: null,
  attempted_at: null,
  last_error: null,
  error_status: null,
  backoff_until: null,
  recovery_at: null,
  ...overrides
})

// The user's live shape: two Go accounts, one fresh/covered and one weekly-spent/metered.
const goGroup = (overrides: Partial<AccountUsageGroup> = {}): AccountUsageGroup => ({
  provider: 'opencode-go',
  fetched_at: '2026-08-28T09:59:30Z',
  age_s: null,
  error: null,
  backoff_until: null,
  windows: [],
  hint: null,
  multi_account: true,
  serving: 'sha256:bbbbbb',
  serving_since: '2026-08-28T09:00:00Z',
  states: { covered: 1, overflow: 1, exhausted: 0 },
  strategy: 'quota_aware',
  accounts: [
    account({
      fingerprint: 'sha256:bbbbbb',
      label: 'OPENCODE_GO_API_KEY_2',
      state: 'covered',
      windows: [
        { period: '5h', used_percent: 1, reset_at: null },
        { period: '7d', used_percent: 0, reset_at: null }
      ]
    }),
    account({
      fingerprint: 'sha256:aaaaaa',
      label: 'OPENCODE_GO_API_KEY',
      state: 'overflow',
      metered: true,
      windows: [
        { period: '7d', used_percent: 100, reset_at: null },
        { period: 'monthly', used_percent: 86, reset_at: null }
      ]
    })
  ],
  ...overrides
})

const baseProps = {
  bgCount: 0,
  busy: false,
  cols: 200,
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

// Flat text of every StatusRule child row. The capacity rows are components whose own children are
// computed inside, so textContent alone would miss them — render each to its flat text.
const renderRows = (element: React.ReactElement<any>) =>
  React.Children.toArray(element.props.children)
    .map(row => {
      const r = row as React.ReactElement<any>
      const isCapacity = r.props && ('quotaGroups' in r.props || 'rightQuota' in r.props)

      return isCapacity ? textContent((r.type as (p: unknown) => ReactNodeLike)(r.props)) : textContent(r)
    })
    .join('\n')

// `ctxLabel`-free usage keeps the primary/secondary rows as plain `CapacityRow`s (a capacity
// Table appears only with a context cell), so each row's `quotaGroups` prop stays inspectable.
const noCtxUsage = (overrides: Record<string, unknown> = {}) => ({
  account_usage_all: [goGroup()],
  account_usage_active: 'opencode-go',
  account_usage_active_account: 'sha256:bbbbbb',
  calls: 0,
  context_max: null,
  context_percent: null,
  context_used: null,
  input: 0,
  output: 0,
  total: 0,
  ...overrides
})

describe('expandAccountGroups', () => {
  it('splits a multi-account group into one labelled row per account, in wire (priority) order', () => {
    const rows = expandAccountGroups([goGroup()])

    expect(rows).toHaveLength(2)
    expect(rows.map(row => row.quotaLabel)).toEqual(['go:1', 'go:2'])
    expect(rows.map(row => row.provider)).toEqual(['opencode-go', 'opencode-go'])
    expect(rows.every(row => row.accounts?.length === 0)).toBe(true)
    expect(rows.every(row => row.multi_account === null)).toBe(true)
    // Each synthetic row carries THAT account's own windows, never a blend.
    expect(rows[0]!.windows).toEqual([
      { period: '5h', used_percent: 1, reset_at: null },
      { period: '7d', used_percent: 0, reset_at: null }
    ])
    expect(rows[1]!.windows).toEqual([
      { period: '7d', used_percent: 100, reset_at: null },
      { period: 'monthly', used_percent: 86, reset_at: null }
    ])
    expect(rows[0]!.quotaAccount?.fingerprint).toBe('sha256:bbbbbb')
    expect(rows[1]!.quotaAccount?.fingerprint).toBe('sha256:aaaaaa')
  })

  it('passes a 0- or 1-account group through unchanged, adding or dropping nothing', () => {
    const single = goGroup({ accounts: [goGroup().accounts![0]!] })
    const legacy: AccountUsageGroup = { ...goGroup(), provider: 'openai-codex', accounts: undefined }

    const out = expandAccountGroups([single, legacy])

    expect(out).toHaveLength(2)
    expect(out[0]).toBe(single)
    expect(out[1]).toBe(legacy)
  })
})

// A plain single-account provider, for the wire-order sweep below.
const otherGroup = (provider: string, pct: number): AccountUsageGroup => ({
  provider,
  fetched_at: '2026-08-28T09:59:30Z',
  age_s: 60,
  error: null,
  backoff_until: null,
  windows: [{ period: '7d', used_percent: pct, reset_at: null }],
  hint: null,
  multi_account: null,
  serving: null,
  serving_since: null,
  states: null,
  strategy: null,
  accounts: []
})

// The wire order is LABEL_ORDER (anthropic, openai-codex, opencode-go): the pool sits last and its
// accounts expand in place there. The aligned table's right column is far narrower than a single
// full-width row, so the table must only engage while it still shows every window number that row
// would - otherwise the session's own account loses one (the real order hid its `5h` at 56 cols).
describe('aligned table keeps the serving account windows (real wire order)', () => {
  it('shows all three windows of the session account from 46 columns up', () => {
    const pool = goGroup({
      accounts: [
        account({
          fingerprint: 'sha256:bbbbbb',
          label: 'OPENCODE_GO_API_KEY_2',
          state: 'covered',
          windows: [
            { period: '5h', used_percent: 11, reset_at: null },
            { period: '7d', used_percent: 4, reset_at: null },
            { period: 'monthly', used_percent: 18, reset_at: null }
          ]
        }),
        account({
          fingerprint: 'sha256:aaaaaa',
          label: 'OPENCODE_GO_API_KEY',
          state: 'overflow',
          metered: true,
          windows: [
            { period: '5h', used_percent: 2, reset_at: null },
            { period: '7d', used_percent: 0, reset_at: null },
            { period: 'monthly', used_percent: 87, reset_at: null }
          ]
        })
      ]
    })

    const missing: number[] = []

    for (let cols = 46; cols <= 170; cols++) {
      const text = renderRows(
        StatusRule({
          ...baseProps,
          cols,
          usage: {
            ...baseProps.usage,
            account_usage_all: [otherGroup('anthropic', 40), otherGroup('openai-codex', 42), pool],
            account_usage_active: 'opencode-go',
            account_usage_active_account: 'sha256:bbbbbb'
          }
        } as any)
      )

      const ctxRow = text.split('\n').find(row => row.includes('go:1')) ?? ''

      if (!(ctxRow.includes('5h 11%') && ctxRow.includes('7d 4%') && ctxRow.includes('mo 18%'))) {
        missing.push(cols)
      }
    }

    expect(missing).toEqual([])
  })
})

describe('layoutQuotaGroups expanded rows', () => {
  it('renders one labelled row per account with its own numbers and badge, never `·multi`', () => {
    const rows = expandAccountGroups([goGroup()])
    const text = joined(layoutQuotaGroups(rows, 9999, DEFAULT_THEME, NOW, 'opencode-go'))

    expect(text).toContain('go:1')
    expect(text).toContain('go:2')
    expect(text).toContain('5h 1%')
    expect(text).toContain('7d 0%')
    expect(text).toContain('7d 100%')
    expect(text).toContain('mo 86%')
    // The overflow account keeps its own badge; the offset account says nothing.
    expect(text).toContain('overflow(metered)')
    expect(text).not.toContain('multi')
    expect(text).not.toContain('2 accounts')
    expect(text).not.toContain('err')
  })

  it('marks the account the row speaks for, never the provider line, for a read error', () => {
    const rows = expandAccountGroups([
      goGroup({
        accounts: [
          account({
            fingerprint: 'sha256:aaaaaa',
            state: 'covered',
            read_health: 'error',
            error_status: 429,
            windows: [{ period: '7d', used_percent: 40, reset_at: null }]
          })
        ]
      })
    ])

    expect(joined(layoutQuotaGroups(rows, 9999, DEFAULT_THEME, NOW))).toContain('·429')
  })

  it('drops later account rows before earlier ones when entries must go', () => {
    const three = goGroup({
      accounts: [
        account({
          fingerprint: 'sha256:bbbbbb',
          state: 'covered',
          windows: [{ period: '7d', used_percent: 1, reset_at: null }]
        }),
        account({
          fingerprint: 'sha256:aaaaaa',
          state: 'overflow',
          windows: [{ period: '7d', used_percent: 100, reset_at: null }]
        }),
        account({
          fingerprint: 'sha256:cccccc',
          state: 'exhausted',
          windows: [{ period: '7d', used_percent: 100, reset_at: null }]
        })
      ]
    })

    const secondary = expandAccountGroups([three]).slice(1) // go:2, go:3
    const budget = joined(layoutQuotaGroups([secondary[0]!], 9999, DEFAULT_THEME, NOW)).length
    const text = joined(layoutQuotaGroups(secondary, budget, DEFAULT_THEME, NOW))

    expect(text).toContain('go:2')
    expect(text).not.toContain('go:3')
  })
})

describe('legacy multi-account fallback', () => {
  it('renders `·N accounts` for a pool with no per-account data, never `·multi` or `err`', () => {
    const legacy: AccountUsageGroup = {
      ...goGroup(),
      provider: 'opencode-go',
      accounts: undefined,
      multi_account: true,
      multi_account_count: 2,
      fetched_at: null
    }

    const text = joined(layoutQuotaGroups([legacy], 9999, DEFAULT_THEME, NOW, 'opencode-go'))

    expect(text).toContain('·2 accounts')
    expect(text).not.toContain('·multi')
    expect(text).not.toContain('err')
  })

  it('omits the marker when the pool size is unknown', () => {
    const legacy: AccountUsageGroup = {
      ...goGroup(),
      accounts: undefined,
      multi_account: true,
      multi_account_count: null,
      windows: [{ period: '7d', used_percent: 5, reset_at: null }]
    }

    const text = joined(layoutQuotaGroups([legacy], 9999, DEFAULT_THEME, NOW, 'opencode-go'))

    expect(text).toContain('7d 5%')
    expect(text).not.toContain('accounts')
    expect(text).not.toContain('·multi')
  })
})

describe('StatusRule expanded multi-account pool', () => {
  it('renders one row per account, each with its own numbers, and no `multi` / sub-rows', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(NOW))

    const element = StatusRule({ ...baseProps, cols: 200, usage: { ...baseProps.usage, ...noCtxUsage() } })
    const rendered = renderRows(element)

    expect(rendered).toContain('go:1')
    expect(rendered).toContain('go:2')
    expect(rendered).toContain('5h 1%')
    expect(rendered).toContain('7d 0%')
    expect(rendered).toContain('7d 100%')
    expect(rendered).toContain('mo 86%')
    expect(rendered).not.toContain('multi')
    expect(rendered).not.toContain('err')
    // No per-account sub-rows: status + primary capacity + secondary capacity only.
    expect(React.Children.toArray(element.props.children)).toHaveLength(3)

    vi.useRealTimers()
  })

  it('gives the session-active account the prime cell and keeps the other at its wire position', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(NOW))

    const element = StatusRule({
      ...baseProps,
      cols: 200,
      usage: { ...baseProps.usage, ...noCtxUsage({ account_usage_active_account: 'sha256:aaaaaa' }) }
    })

    const rows = React.Children.toArray(element.props.children)
    const prime = rows[1] as React.ReactElement<any>
    const secondary = rows[2] as React.ReactElement<any>

    const labels = (row: React.ReactElement<any>) =>
      row.props.quotaGroups.map((group: AccountUsageGroup & { quotaLabel?: string }) => group.quotaLabel)

    // aaaaaa is accounts[1] → `go:2` is the session's account; it owns the prime cell.
    expect(labels(prime)).toEqual(['go:2'])
    // The other account keeps its wire position below.
    expect(labels(secondary)).toEqual(['go:1'])

    vi.useRealTimers()
  })

  it('never drops the session account while later entries drop from the end', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(NOW))

    const anthropic = {
      ...goGroup(),
      provider: 'anthropic',
      accounts: undefined,
      multi_account: null,
      windows: [{ period: '7d', used_percent: 3, reset_at: null }]
    }

    const element = StatusRule({
      ...baseProps,
      cols: 28,
      usage: { ...baseProps.usage, ...noCtxUsage({ account_usage_all: [goGroup(), anthropic] }) }
    })

    const rendered = renderRows(element)

    // The session's own account row survives at the narrow width even as the trailing entries go.
    expect(rendered).toContain('go:1')
    expect(rendered).toContain('5h 1%')
    expect(rendered).not.toContain('go:2')

    vi.useRealTimers()
  })
})

// Cross-vendor review repro (b4-expand-accounts): two Go accounts, the session's own account is
// `go:2` (fingerprint b) — the metered/overflow one with a fresh read and a 5h/7d/monthly window
// set wide enough that no candidate layout fits a narrow terminal. The old ladder returned [] and
// cleared the row, hiding the live metered account while the non-session `go:1` survived.
const narrowAccount = (fingerprint: string, used: number, state = 'covered', metered = false): AccountUsageAccount =>
  account({
    fingerprint,
    label: 'key',
    metered,
    read_health: 'ok',
    windows: [
      { period: '5h', used_percent: used, reset_at: null },
      { period: '7d', used_percent: used, reset_at: null },
      { period: 'monthly', used_percent: used, reset_at: null }
    ],
    fetched_at: '2026-10-12T03:00:00Z',
    age_s: 0,
    state
  })

const narrowPool = (): AccountUsageGroup => ({
  provider: 'opencode-go',
  fetched_at: null,
  age_s: null,
  error: null,
  backoff_until: null,
  windows: [],
  hint: null,
  multi_account: true,
  serving: 'sha256:b',
  accounts: [narrowAccount('sha256:a', 1), narrowAccount('sha256:b', 100, 'overflow', true)]
})

const narrowUsage = (activeAccount: string | null) =>
  noCtxUsage({
    account_usage_all: [narrowPool()],
    account_usage_active: 'opencode-go',
    account_usage_active_account: activeAccount
  })

const narrowFrames = (cols: number, activeAccount: string | null) =>
  renderRows(StatusRule({ ...baseProps, cols, usage: { ...baseProps.usage, ...narrowUsage(activeAccount) } }))

describe('session account survives a narrow terminal', () => {
  it('keeps the session account and its badge at 38 columns without context', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(NOW))

    const rendered = narrowFrames(38, 'sha256:b')

    // The session's own account must not vanish while the non-session one survives.
    expect(rendered).toContain('go:1')
    expect(rendered).toContain('go:2')
    expect(rendered).toContain('overflow(metered)')

    vi.useRealTimers()
  })

  it('keeps the session account identity at every width from 28 to 160 columns', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(NOW))

    const missing: number[] = []

    for (let cols = 28; cols <= 160; cols++) {
      if (!narrowFrames(cols, 'sha256:b').includes('go:2')) {
        missing.push(cols)
      }
    }

    expect(missing).toEqual([])

    vi.useRealTimers()
  })

  it('shows both accounts for a matching, null, or mismatched session fingerprint', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(NOW))

    for (const fp of ['sha256:a', 'sha256:b', null, 'sha256:missing']) {
      const rendered = narrowFrames(38, fp)

      expect(rendered, `fingerprint ${fp}`).toContain('go:1')
      expect(rendered, `fingerprint ${fp}`).toContain('go:2')
    }

    vi.useRealTimers()
  })

  it('never shows an account number on the wrong account row or `·N accounts` on an expanded pool', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(NOW))

    const rendered = narrowFrames(38, 'sha256:b')

    // go:1 is the covered account (all windows 1%); go:2 is the metered overflow account (100%).
    expect(rendered).toContain('5h 1%')
    expect(rendered).not.toContain('100%')
    expect(rendered).not.toContain('accounts')
    expect(rendered).not.toContain('multi')

    vi.useRealTimers()
  })
})

// A provider with all three windows, for the secondary-row fidelity check below.
const threeWindows = (provider: string, a: number, b: number, c: number): AccountUsageGroup => ({
  ...otherGroup(provider, a),
  windows: [
    { period: '5h', used_percent: a, reset_at: null },
    { period: '7d', used_percent: b, reset_at: null },
    { period: 'monthly', used_percent: c, reset_at: null }
  ]
})

describe('closing the aligned table keeps the secondary windows', () => {
  it('accepts the secondary 5h loss to keep the session account windows', () => {
    const pool = goGroup({
      accounts: [
        account({
          fingerprint: 'sha256:bbbbbb',
          label: 'OPENCODE_GO_API_KEY_2',
          state: 'covered',
          windows: [
            { period: '5h', used_percent: 11, reset_at: null },
            { period: '7d', used_percent: 4, reset_at: null },
            { period: 'monthly', used_percent: 18, reset_at: null }
          ]
        }),
        account({
          fingerprint: 'sha256:aaaaaa',
          label: 'OPENCODE_GO_API_KEY',
          state: 'overflow',
          metered: true,
          windows: [
            { period: '5h', used_percent: 2, reset_at: null },
            { period: '7d', used_percent: 0, reset_at: null },
            { period: 'monthly', used_percent: 87, reset_at: null }
          ]
        })
      ]
    })

    const text = renderRows(
      StatusRule({
        ...baseProps,
        cols: 60,
        usage: {
          ...baseProps.usage,
          account_usage_all: [threeWindows('anthropic', 40, 41, 42), threeWindows('openai-codex', 42, 43, 44), pool],
          account_usage_active: 'opencode-go',
          account_usage_active_account: 'sha256:bbbbbb'
        }
      } as any)
    )

    // The accepted trade at this width: the session's own account keeps every window (the aligned
    // table would have hidden `5h 11%`), while the secondary row degrades under the signed policy
    // - it drops the providers' 5h windows, not the providers - so both stay visible.
    expect(text).toContain('5h 11%')
    expect(text).toContain('A\\ 7d 41%')
    expect(text).toContain('codex 7d 43%')
    expect(text).not.toContain('5h 40%') // the trade: the secondary 5h is what pays for it
  })
})
