import assert from 'node:assert/strict'
import test from 'node:test'
import { apply } from '../plugins/atri-tools/index.js'

test('audit query exposes bounded historical filters without relaxing guild permissions', () => {
  const tools = []
  apply({ tools: { register(tool) { tools.push(tool) } } })
  const query = tools.find(tool => tool.name === 'discord_query')
  const schema = JSON.stringify(query)
  for (const key of ['audit_action', 'actor_id', 'target_id', 'query', 'since', 'until', 'cursor']) {
    assert.ok(schema.includes(`"${key}"`), key)
  }
  for (const phrase of ['45-day', 'View Audit Log', 'Bot-developer status alone', 'Empty', 'next_cursor']) {
    assert.ok(schema.toLowerCase().includes(phrase.toLowerCase()), phrase)
  }
  assert.ok(!schema.includes('"guild_id"'))
})
