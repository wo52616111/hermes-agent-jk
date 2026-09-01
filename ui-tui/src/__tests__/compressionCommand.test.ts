import { beforeEach, describe, expect, it, vi } from 'vitest'

import { sessionCommands } from '../app/slash/commands/session.js'
import { getUiState, patchUiState } from '../app/uiStore.js'

const compressCommand = sessionCommands.find(command => command.name === 'compress')!

const guarded =
  <T>(fn: (response: T) => void) =>
  (response: null | T) => {
    if (response) {
      fn(response)
    }
  }

describe('/compress slash command', () => {
  beforeEach(() => {
    patchUiState({ busy: false, status: '' })
  })

  it('shows the live busy indicator until compression completes', async () => {
    let resolve!: (value: unknown) => void

    const pending = new Promise(resolvePromise => {
      resolve = resolvePromise
    })

    const rpc = vi.fn(() => pending)

    const ctx = {
      gateway: { rpc },
      guarded,
      guardedErr: vi.fn(),
      sid: 'sid-1',
      stale: () => false,
      transcript: { setHistoryItems: vi.fn(), sys: vi.fn() }
    }

    compressCommand.run('', ctx as any, 'compress')

    expect(getUiState()).toMatchObject({ busy: true, status: 'compressing…' })

    resolve({ messages: [], info: null, usage: null, summary: null, removed: 0 })
    await pending
    await Promise.resolve()
    await Promise.resolve()

    expect(getUiState()).toMatchObject({ busy: false, status: '' })
  })

  it('clears the live busy indicator when compression fails', async () => {
    const rpc = vi.fn(() => Promise.reject(new Error('compression failed')))

    const ctx = {
      gateway: { rpc },
      guarded,
      guardedErr: vi.fn(),
      sid: 'sid-1',
      stale: () => false,
      transcript: { setHistoryItems: vi.fn(), sys: vi.fn() }
    }

    compressCommand.run('', ctx as any, 'compress')
    await new Promise(resolvePromise => setTimeout(resolvePromise, 0))

    expect(getUiState()).toMatchObject({ busy: false, status: '' })
  })
})
