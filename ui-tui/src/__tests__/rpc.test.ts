import { describe, expect, it } from 'vitest'

import { asRpcResult, rpcErrorMessage } from '../lib/rpc.js'

describe('asRpcResult', () => {
  it('keeps plain object payloads', () => {
    expect(asRpcResult({ ok: true, value: 'x' })).toEqual({ ok: true, value: 'x' })
  })

  it('rejects missing or non-object payloads', () => {
    expect(asRpcResult(undefined)).toBeNull()
    expect(asRpcResult(null)).toBeNull()
    expect(asRpcResult('oops')).toBeNull()
    expect(asRpcResult(['bad'])).toBeNull()
  })
})

describe('rpcErrorMessage', () => {
  it('prefers Error messages', () => {
    expect(rpcErrorMessage(new Error('boom'))).toBe('boom')
  })

  it('falls back for unknown errors', () => {
    expect(rpcErrorMessage('broken')).toBe('broken')
    expect(rpcErrorMessage({ code: 500 })).toBe('request failed')
  })

  it('prints a refusal\'s data.detail under its bare reason code', () => {
    const refusal = Object.assign(new Error('model_resolution_failed'), {
      data: { detail: 'DeepSeek is not connected. Set DEEPSEEK_API_KEY.', reason: 'model_resolution_failed' }
    })

    expect(rpcErrorMessage(refusal)).toBe('model_resolution_failed\nDeepSeek is not connected. Set DEEPSEEK_API_KEY.')
  })

  it('leaves an error whose message is not the reason code alone', () => {
    const other = Object.assign(new Error('boom'), { data: { detail: 'x', reason: 'other' } })

    expect(rpcErrorMessage(other)).toBe('boom')
  })
})
