import { describe, expect, it } from 'vitest'

import { initialHistoryItems } from '../app/useMainApp.js'

describe('initial transcript', () => {
  it('renders a fallback intro before session information is available', () => {
    expect(initialHistoryItems()).toEqual([{ kind: 'intro', role: 'system', text: '' }])
  })
})
