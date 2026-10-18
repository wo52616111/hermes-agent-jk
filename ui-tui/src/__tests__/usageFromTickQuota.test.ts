import type { Usage } from '@hermes/shared/gateway-events'
import { describe, expect, it } from 'vitest'

import { usageFromTick } from '../domain/usage.js'

const group = {
  provider: 'anthropic',
  windows: [{ period: '5h', used_percent: 12, reset_at: '2026-10-18T09:00:00+00:00' }]
}

// A canonical `session.usage` frame is the owner's `session.stats` dict: counters and the capacity
// row together (`gateway/session_usage.py::quota_fields`). The status bar renders quota from THIS
// object, so a tick that drops the quota keys leaves the provider line blank for the whole session.
describe('usageFromTick quota passthrough (canonical session.usage)', () => {
  it('carries every account_usage key next to the counters', () => {
    const usage = usageFromTick({
      api_calls: 3,
      total_tokens: 1600,
      input_tokens: 1200,
      output_tokens: 400,
      account_usage_all: [group],
      account_usage: group,
      account_usage_active: 'anthropic',
      account_usage_active_account: 'fp-1'
    }) as Usage

    expect(usage.calls).toBe(3)
    expect(usage.total).toBe(1600)
    expect(usage.account_usage_all).toEqual([group])
    expect(usage.account_usage_active).toBe('anthropic')
    expect(usage.account_usage_active_account).toBe('fp-1')
  })

  it('paints a quota-only frame (resumed session, no model call yet) without zeroing counters', () => {
    const usage = usageFromTick({ account_usage_all: [group], account_usage_active: 'anthropic' }) as Usage

    expect(usage.account_usage_all).toEqual([group])
    // Counters are absent, not zero: merging this patch must not clobber the session's totals.
    expect(usage.calls).toBeUndefined()
    expect(usage.total).toBeUndefined()
  })

  it('still ignores a frame with neither counters nor quota', () => {
    expect(usageFromTick({ model: 'x' })).toBeUndefined()
    expect(usageFromTick(null)).toBeUndefined()
  })

  it('keeps the legacy `usage` envelope shape untouched', () => {
    const usage = usageFromTick({ usage: { calls: 1, total: 10, account_usage_active: 'anthropic' } }) as Usage

    expect(usage).toEqual({ calls: 1, total: 10, account_usage_active: 'anthropic' })
  })
})
