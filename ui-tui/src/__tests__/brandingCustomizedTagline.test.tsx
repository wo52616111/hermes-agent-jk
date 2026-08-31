import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { Banner } from '../components/branding.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

async function renderBanner(columns = 80): Promise<string> {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  Object.assign(stdout, { columns, isTTY: false, rows: 40 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })

  let captured = ''
  stdout.on('data', chunk => {
    captured += chunk.toString()
  })

  const instance = renderSync(React.createElement(Banner, { maxWidth: columns, t: DEFAULT_THEME }), {
    patchConsole: false,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })

  try {
    await delay(20)

    return stripAnsi(captured)
  } finally {
    instance.unmount()
    instance.cleanup()
  }
}

describe('customized TUI banner', () => {
  it('identifies this downstream build in the startup tagline', async () => {
    const frame = await renderBanner()

    expect(frame).toContain('Nous Research · Messenger of the Digital Gods · Customized by junkai')
  })
})
