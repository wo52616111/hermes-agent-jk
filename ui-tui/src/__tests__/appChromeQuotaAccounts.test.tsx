import { stringWidth } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import {
  accountStaleMarker,
  layoutQuotaAccounts,
  layoutQuotaGroups,
  type QuotaSegment,
  quotaStaleMarker,
  StatusRule
} from '../components/appChrome.js'
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
  overrides: Partial<AccountUsageAccount> & Pick<AccountUsageAccount, 'fingerprint' | 'read_health' | 'state'>
): AccountUsageAccount => ({
  label: 'OPENCODE_GO_API_KEY',
  entry_id: null,
  metered: false,
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

describe('quotaStaleMarker multi-account marker', () => {
  it('renders a meaningful marker instead of the literal `err` for a multi_account group', () => {
    // The wire ships `{status: 0, message: "multi_account"}`; `status || 'err'` reads 0 as falsy.
    expect(quotaStaleMarker({ age_s: null, error: { status: 0, message: 'multi_account' }, multi_account: true })).toBe(
      '·multi'
    )
    expect(quotaStaleMarker({ age_s: null, error: { status: 429, message: 'rate limited' }, multi_account: null })).toBe(
      '·429'
    )
  })
})

describe('layoutQuotaGroups per-account provider line', () => {
  it('shows the account count and no inline percentage while the sub-rows have room', () => {
    const group = goGroup()
    const text = joined(layoutQuotaGroups([group], 9999, DEFAULT_THEME, NOW, 'opencode-go', true, 'sha256:bbbbbb'))

    expect(text).toContain('go ·2 accounts')
    expect(text).not.toContain('err')
    // With room the numbers live on the sub-rows, unsummed - the provider line carries none.
    expect(text).not.toContain('%')
    expect(layoutQuotaAccounts(group, 'sha256:bbbbbb', 9999, DEFAULT_THEME, NOW)).toHaveLength(2)
  })

  it("falls back to the SERVING account's own numbers when the sub-rows are trimmed", () => {
    const group = goGroup()
    // A budget too small for the sub-rows: the provider line must carry the SERVING account's
    // own windows (bbbbbb: 5h 1%, 7d 0%) - never a sum, never the other account's numbers.
    const trimmed = joined(layoutQuotaGroups([group], 24, DEFAULT_THEME, NOW, 'opencode-go', true, 'sha256:bbbbbb'))

    expect(trimmed).toBe(' │ go 5h 1%·7d 0%')
    expect(trimmed).not.toContain('100%')
    expect(trimmed).not.toContain('86%')
    expect(trimmed).not.toContain('2 accounts')
  })

  it('puts the session-active account state badge on the provider line', () => {
    const text = joined(layoutQuotaGroups([goGroup()], 9999, DEFAULT_THEME, NOW, 'opencode-go', true, 'sha256:aaaaaa'))

    expect(text).toContain('overflow(metered)')
  })

  it('keeps the provider-line badge on a narrow terminal that drops the account count', () => {
    // The line speaks for the SERVING account (aaaaaa: overflow(metered)); a budget that fits
    // `│ go overflow(metered)` but not that account's own numbers keeps the badge, drops the count.
    const group = goGroup({
      serving: 'sha256:aaaaaa',
      accounts: [
        account({
          fingerprint: 'sha256:aaaaaa',
          label: 'OPENCODE_GO_API_KEY',
          metered: true,
          read_health: 'ok',
          state: 'overflow',
          windows: [
            { period: '7d', used_percent: 100, reset_at: null },
            { period: 'monthly', used_percent: 100, reset_at: null }
          ]
        }),
        account({
          fingerprint: 'sha256:bbbbbb',
          label: 'OPENCODE_GO_API_KEY_2',
          read_health: 'ok',
          state: 'covered',
          windows: [{ period: '5h', used_percent: 1, reset_at: null }]
        })
      ]
    })

    const text = joined(
      layoutQuotaGroups([group], stringWidth(' │ go overflow(metered)'), DEFAULT_THEME, NOW, 'opencode-go', true, 'sha256:bbbbbb')
    )

    expect(text).toContain('overflow(metered)')
    expect(text).not.toContain('2 accounts')
  })

  it('speaks for ONE account on the trimmed provider line: the serving account numbers AND its badge', () => {
    // Session is on aaaaaa (overflow); the pool serves from bbbbbb (exhausted). With the sub-rows
    // trimmed (subRowsBudget 0) the provider line must carry bbbbbb's own windows AND bbbbbb's
    // badge - one account, never a mix, never a sum.
    const group = goGroup({
      serving: 'sha256:bbbbbb',
      accounts: [
        account({
          fingerprint: 'sha256:bbbbbb',
          label: 'OPENCODE_GO_API_KEY_2',
          read_health: 'ok',
          state: 'exhausted',
          windows: [
            { period: '5h', used_percent: 3, reset_at: null },
            { period: '7d', used_percent: 4, reset_at: null }
          ]
        }),
        account({
          fingerprint: 'sha256:aaaaaa',
          label: 'OPENCODE_GO_API_KEY',
          metered: true,
          read_health: 'ok',
          state: 'overflow',
          windows: [
            { period: '7d', used_percent: 100, reset_at: null },
            { period: 'monthly', used_percent: 86, reset_at: null }
          ]
        })
      ]
    })

    const text = joined(layoutQuotaGroups([group], 9999, DEFAULT_THEME, NOW, 'opencode-go', true, 'sha256:aaaaaa', 0))

    expect(text).toContain('5h 3%')
    expect(text).toContain('7d 4%')
    expect(text).toContain('exhausted')
    expect(text).not.toContain('100%')
    expect(text).not.toContain('86%')
    expect(text).not.toContain('overflow')
    expect(text).not.toContain('2 accounts')
  })

  it('renders a legacy multi-account group as ·multi, never ·err', () => {
    const legacy: AccountUsageGroup = {
      provider: 'opencode-go',
      fetched_at: null,
      age_s: null,
      error: { status: 0, message: 'multi_account' },
      backoff_until: null,
      windows: [],
      hint: null,
      multi_account: true
    }

    const text = joined(layoutQuotaGroups([legacy], 9999, DEFAULT_THEME, NOW, 'opencode-go'))

    expect(text).toContain('·multi')
    expect(text).not.toContain('·err')
  })
})

describe('layoutQuotaAccounts sub-rows', () => {
  it('renders one sub-row per account with its own windows', () => {
    const rows = layoutQuotaAccounts(goGroup(), 'sha256:bbbbbb', 9999, DEFAULT_THEME, NOW)

    expect(rows).toHaveLength(2)
    const [first, second] = rows.map(row => joined(row.segments))

    expect(first).toContain('OPENCODE_GO_API_KEY_2')
    expect(first).toContain('covered')
    expect(first).toContain('5h 1%')
    expect(first).toContain('7d 0%')
    expect(second).toContain('OPENCODE_GO_API_KEY')
    expect(second).toContain('overflow(metered)')
    expect(second).toContain('7d 100%')
    expect(second).toContain('mo 86%')
    // No blended provider-level percentage leaks into either row.
    expect(first).not.toContain('43%')
  })

  it('bolds and points at the account this session is on', () => {
    const rows = layoutQuotaAccounts(goGroup(), 'sha256:bbbbbb', 9999, DEFAULT_THEME, NOW)

    expect(rows[0]!.active).toBe(true)
    expect(rows[0]!.segments.some(segment => segment.bold)).toBe(true)
    expect(joined(rows[0]!.segments)).toContain('▸')
    expect(rows[1]!.active).toBe(false)
    expect(rows[1]!.segments.every(segment => !segment.bold)).toBe(true)
    expect(joined(rows[1]!.segments)).not.toContain('▸')
  })

  it('falls back to the serving account when the session-active account is unknown', () => {
    const rows = layoutQuotaAccounts(goGroup(), null, 9999, DEFAULT_THEME, NOW)

    expect(rows.find(row => row.active)?.segments.map(segment => segment.text).join('')).toContain(
      'OPENCODE_GO_API_KEY_2'
    )
  })

  it('drops the other accounts before the active account windows on a narrow terminal', () => {
    const full = layoutQuotaAccounts(goGroup(), 'sha256:bbbbbb', 9999, DEFAULT_THEME, NOW)
    const activeRow = joined(full[0]!.segments)
    const narrow = layoutQuotaAccounts(goGroup(), 'sha256:bbbbbb', stringWidth(activeRow), DEFAULT_THEME, NOW)

    expect(narrow).toHaveLength(1)
    expect(joined(narrow[0]!.segments)).toBe(activeRow)
  })

  it('keeps every sub-row that fits on its own even when their widths sum past the budget', () => {
    // Two independent sub-rows (own Boxes, no shared horizontal budget): widths 41 and 51 each
    // fit in 80, but their sum (92) does not - summing the widths would discard the layout and
    // hide an account's state and numbers on an 80-column terminal.
    const group = goGroup({
      accounts: [
        account({
          fingerprint: 'sha256:aaaaaa',
          label: 'A'.repeat(21),
          read_health: 'ok',
          state: 'covered',
          windows: [{ period: '7d', used_percent: 0, reset_at: null }]
        }),
        account({
          fingerprint: 'sha256:bbbbbb',
          label: 'B'.repeat(28),
          read_health: 'ok',
          state: 'overflow',
          windows: [{ period: '7d', used_percent: 100, reset_at: null }]
        })
      ]
    })

    const rows = layoutQuotaAccounts(group, null, 80, DEFAULT_THEME, NOW)
    const widths = rows.map(row => row.segments.reduce((sum, segment) => sum + stringWidth(segment.text), 0))

    expect(rows).toHaveLength(2)
    expect(widths).toEqual([41, 51])
    expect(Math.max(...widths)).toBeLessThanOrEqual(80)
    expect(widths.reduce((sum, width) => sum + width, 0)).toBeGreaterThan(80)
  })

  it('returns nothing for a group without a per-account view', () => {
    expect(layoutQuotaAccounts({ ...goGroup(), accounts: undefined }, null, 9999, DEFAULT_THEME, NOW)).toEqual([])
  })
})

describe('accountStaleMarker', () => {
  it('marks a stale read with its compact age and a 429 read with ·429', () => {
    expect(accountStaleMarker({ read_health: 'ok', age_s: 45 * 60, error_status: null })).toBe('·45m')
    expect(accountStaleMarker({ read_health: 'ok', age_s: 60, error_status: null })).toBe('')
    expect(accountStaleMarker({ read_health: 'error', age_s: null, error_status: 429 })).toBe('·429')
    expect(accountStaleMarker({ read_health: 'error', age_s: null, error_status: 500 })).toBe('·err')
  })

  it('renders the compact age on a sub-row once the idle clock re-aged the account', () => {
    // The gateway's idle clock re-ages each account from its OWN fetched_at, so a 45-minute-old
    // read (age_s 2700) surfaces as `·45m` on the account's sub-row - never the provider's age.
    const stale = goGroup({
      accounts: [
        account({
          fingerprint: 'sha256:bbbbbb',
          label: 'OPENCODE_GO_API_KEY_2',
          read_health: 'ok',
          state: 'covered',
          age_s: 45 * 60,
          windows: [{ period: '7d', used_percent: 1, reset_at: null }]
        })
      ]
    })

    const rows = layoutQuotaAccounts(stale, 'sha256:bbbbbb', 9999, DEFAULT_THEME, NOW)

    expect(joined(rows[0]!.segments)).toContain('·45m')
  })
})

describe('StatusRule per-account sub-rows', () => {
  it('renders the provider line plus a sub-row per account', () => {
    const element = StatusRule({
      ...baseProps,
      usage: {
        ...baseProps.usage,
        account_usage_all: [goGroup()],
        account_usage_active: 'opencode-go',
        account_usage_active_account: 'sha256:bbbbbb'
      }
    })

    const rows = React.Children.toArray(element.props.children)
    // The capacity row is a child component; render it to its flat text.
    const capacity = rows[1] as React.ReactElement<any>
    const capacityText = textContent((capacity.type as (p: unknown) => ReactNodeLike)(capacity.props))
    const subRows = textContent(element)

    expect(capacityText).toContain('go ·2 accounts')
    expect(subRows).toContain('OPENCODE_GO_API_KEY_2')
    expect(subRows).toContain('OPENCODE_GO_API_KEY')
    expect(subRows).not.toContain('err')
    // status row + capacity row + two account sub-rows.
    expect(rows).toHaveLength(4)
  })
})
