import { describe, expect, it } from 'vitest'

import { initialHistoryItems } from '../app/useMainApp.js'

describe('initial transcript', () => {
  it('does not render an intro before session information is available', () => {
    expect(initialHistoryItems()).toEqual([])
  })
})
