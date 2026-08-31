import { describe, expect, it } from 'vitest'

import { toggleComposerStash } from '../domain/composerStash.js'

const draft = {
  input: 'review this [[ Image 1 ]]',
  inputBuf: ['first line', 'second line'],
  queueEditIdx: 2,
  tokens: [{ index: 1, kind: 'image' as const, label: '[[ Image 1 ]]', path: '/tmp/shot.png' }]
}

describe('toggleComposerStash', () => {
  it('moves every composer field into a one-slot stash', () => {
    const result = toggleComposerStash(draft, null)

    expect(result.action).toBe('stashed')
    expect(result.current).toEqual({ input: '', inputBuf: [], queueEditIdx: null, tokens: [] })
    expect(result.stash).toEqual(draft)
    expect(result.stash).not.toBe(draft)
    expect(result.stash?.inputBuf).not.toBe(draft.inputBuf)
    expect(result.stash?.tokens).not.toBe(draft.tokens)
  })

  it('restores the full composer draft and empties the stash slot', () => {
    const stashed = toggleComposerStash(draft, null).stash
    const result = toggleComposerStash({ input: '/model', inputBuf: [], queueEditIdx: null, tokens: [] }, stashed)

    expect(result.action).toBe('restored')
    expect(result.current).toEqual(draft)
    expect(result.stash).toBeNull()
  })

  it('does nothing when the composer and stash are both empty', () => {
    const empty = { input: '', inputBuf: [], queueEditIdx: null, tokens: [] }

    expect(toggleComposerStash(empty, null)).toEqual({ action: 'empty', current: empty, stash: null })
  })
})
