import assert from 'node:assert/strict'
import test from 'node:test'
import { apply } from '../plugins/atri-tools/index.js'

test('log tool advertises history, precise filters, cursors and no arbitrary paths in both modes', () => {
  const previous = process.env.ATRI_AGENT_CODE_MODE
  try {
    for (const mode of ['false', 'true']) {
      process.env.ATRI_AGENT_CODE_MODE = mode
      const tools = []
      apply({ tools: { register(tool) { tools.push(tool) } } })
      const log = tools.find(tool => tool.name === 'runtime_read_log')
      assert.ok(log)
      const schema = JSON.stringify(log)
      for (const key of ['mode', 'stream', 'tail_lines', 'history', 'terms', 'query', 'exclude', 'since', 'until', 'level', 'case_sensitive', 'context_lines', 'limit', 'cursor']) {
        assert.ok(schema.includes(`"${key}"`), key)
      }
      for (const key of ['file_path', 'path', 'shell', 'regex', 'command']) {
        assert.ok(!schema.includes(`"${key}"`), key)
      }
      assert.ok(schema.includes('Owner-only'))
      assert.ok(schema.includes('Empty page with a cursor is not a no-match'))
    }
  } finally {
    if (previous === undefined) delete process.env.ATRI_AGENT_CODE_MODE
    else process.env.ATRI_AGENT_CODE_MODE = previous
  }
})
