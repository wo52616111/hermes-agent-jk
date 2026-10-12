import { stringWidth } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import {
  accountReadMarker,
  accountStaleMarker,
  expandAccountGroups,
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

// Flat text of every StatusRule child row, rendering each capacity component to its own text.
const renderRows = (element: React.ReactElement<any>) =>
  React.Children.toArray(element.props.children)
    .map(row => {
      const r = row as React.ReactElement<any>
      const isCapacity = r.props && ('quotaGroups' in r.props || 'rightQuota' in r.props)

      return isCapacity ? textContent((r.type as (p: unknown) => ReactNodeLike)(r.props)) : textContent(r)
    })
    .join('\n')

describe('quotaStaleMarker multi-account marker', () => {
  it('names the pool size instead of a literal `err`, and never `·multi`', () => {
    // The wire ships `{status: 0, message: "multi_account"}`; `status || 'err'` reads 0 as falsy.
    expect(
      quotaStaleMarker({
        age_s: null,
        error: { status: 0, message: 'multi_account' },
        multi_account: true,
        multi_account_count: 2
      })
    ).toBe('·2 accounts')
    expect(
      quotaStaleMarker({ age_s: null, error: { status: 429, message: 'rate limited' }, multi_account: null })
    ).toBe('·429')
  })

  it('omits the marker when the pool size is unknown', () => {
    expect(
      quotaStaleMarker({
        age_s: null,
        error: { status: 0, message: 'multi_account' },
        multi_account: true,
        multi_account_count: null
      })
    ).toBe('')
  })
})

describe('account markers', () => {
  it('marks a 429 read with ·429 and a failed read with ·err', () => {
    expect(accountReadMarker({ error_status: 429, read_health: 'error' })).toBe('·429')
    expect(accountReadMarker({ error_status: 500, read_health: 'error' })).toBe('·err')
    expect(accountReadMarker({ error_status: null, read_health: 'ok' })).toBe('')
  })

  it('marks a stale read with its compact age', () => {
    expect(accountStaleMarker({ read_health: 'ok', age_s: 45 * 60, error_status: null })).toBe('·45m')
    expect(accountStaleMarker({ read_health: 'ok', age_s: 60, error_status: null })).toBe('')
    expect(accountStaleMarker({ read_health: 'error', age_s: null, error_status: 429 })).toBe('·429')
    expect(accountStaleMarker({ read_health: 'error', age_s: null, error_status: 500 })).toBe('·err')
  })
})

describe('layoutQuotaGroups expanded rows', () => {
  it('carries each account row its own read marker and badge', () => {
    const rows = expandAccountGroups([
      goGroup({
        accounts: [
          account({
            fingerprint: 'sha256:bbbbbb',
            state: 'covered',
            windows: [{ period: '7d', used_percent: 1, reset_at: null }]
          }),
          account({
            fingerprint: 'sha256:aaaaaa',
            state: 'exhausted',
            read_health: 'error',
            error_status: 500,
            windows: [{ period: '7d', used_percent: 100, reset_at: null }]
          })
        ]
      })
    ])

    const text = joined(layoutQuotaGroups(rows, 9999, DEFAULT_THEME, NOW))

    expect(text).toContain('go:1')
    expect(text).toContain('go:2')
    expect(text).toContain('exhausted')
    expect(text).toContain('·err')
  })
})

describe('StatusRule expanded quota composition', () => {
  it('renders one row per account, no per-account sub-rows, and no env-var label', () => {
    const element = StatusRule({
      ...baseProps,
      usage: {
        ...baseProps.usage,
        account_usage_all: [goGroup()],
        account_usage_active: 'opencode-go',
        account_usage_active_account: 'sha256:bbbbbb'
      }
    })

    const rendered = renderRows(element)

    expect(rendered).toContain('go:1')
    expect(rendered).toContain('go:2')
    expect(rendered).toContain('overflow(metered)')
    expect(rendered).not.toContain('OPENCODE_GO_API_KEY')
    expect(rendered).not.toContain('multi')
    expect(rendered).not.toContain('err')
    // No per-account sub-rows: status + primary capacity + secondary capacity only.
    expect(React.Children.toArray(element.props.children)).toHaveLength(3)
  })

  it('names a legacy multi-account pool by its live count', () => {
    const legacy: AccountUsageGroup = {
      ...goGroup(),
      provider: 'opencode-go',
      accounts: undefined,
      multi_account: true,
      multi_account_count: 2,
      fetched_at: null
    }

    const rendered = renderRows(
      StatusRule({
        ...baseProps,
        usage: { ...baseProps.usage, account_usage_all: [legacy], account_usage_active: null }
      })
    )

    expect(rendered).toContain('·2 accounts')
    expect(rendered).not.toContain('·multi')
    expect(rendered).not.toContain('err')
  })

  it('keeps a single-account group numbers on its row (no sub-rows)', () => {
    const single = goGroup({
      windows: [{ period: '7d', used_percent: 12, reset_at: null }],
      accounts: [
        account({
          fingerprint: 'sha256:bbbbbb',
          state: 'covered',
          windows: [{ period: '7d', used_percent: 12, reset_at: null }]
        })
      ],
      multi_account: null
    })

    const rendered = renderRows(
      StatusRule({
        ...baseProps,
        usage: {
          ...baseProps.usage,
          account_usage_all: [single],
          account_usage_active: 'opencode-go',
          account_usage_active_account: 'sha256:bbbbbb'
        }
      })
    )

    expect(rendered).toContain('7d 12%')
    expect(rendered).not.toContain('accounts')
  })

  it('keeps both account rows when the budget exactly fits them', () => {
    const rows = expandAccountGroups([goGroup()])
    const full = ' │ go:1 5h 1%·7d 0% │ go:2 7d 100%·mo 86% overflow(metered)'

    expect(joined(layoutQuotaGroups(rows, stringWidth(full), DEFAULT_THEME, NOW, 'opencode-go'))).toBe(full)
  })
})
