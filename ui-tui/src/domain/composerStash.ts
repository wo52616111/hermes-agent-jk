import type { ComposerToken } from '../app/interfaces.js'

export type ComposerDraft = {
  input: string
  inputBuf: string[]
  queueEditIdx: null | number
  tokens: ComposerToken[]
}

export type ComposerStashAction = 'empty' | 'restored' | 'stashed'

export type ComposerStashResult = {
  action: ComposerStashAction
  current: ComposerDraft
  stash: ComposerDraft | null
}

const copyDraft = (draft: ComposerDraft): ComposerDraft => ({
  ...draft,
  inputBuf: [...draft.inputBuf],
  tokens: [...draft.tokens]
})

export function toggleComposerStash(current: ComposerDraft, stash: ComposerDraft | null): ComposerStashResult {
  if (stash) {
    return { action: 'restored', current: copyDraft(stash), stash: null }
  }

  if (!current.input && !current.inputBuf.length && !current.tokens.length) {
    return { action: 'empty', current, stash: null }
  }

  return {
    action: 'stashed',
    current: { input: '', inputBuf: [], queueEditIdx: null, tokens: [] },
    stash: copyDraft(current)
  }
}
