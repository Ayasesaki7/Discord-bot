import assert from 'node:assert/strict'
import test from 'node:test'
import { apply } from '../plugins/atri-tools/index.js'

test('normal agent schema removes fortune while preserving other tools', () => {
  const previous = process.env.ATRI_AGENT_CODE_MODE
  try {
    process.env.ATRI_AGENT_CODE_MODE = 'false'
    const tools = []
    apply({ tools: { register(tool) { tools.push(tool) } } })
    const names = tools.map(tool => tool.name)
    assert.ok(!names.includes('daily_fortune'))
    for (const name of ['web_search', 'send_voice', 'music_control', 'cosmetic_roles', 'discord_manage', 'discord_query', 'improve_self', 'channel_memory']) {
      assert.ok(names.includes(name), `${name} must remain registered`)
    }
  } finally {
    if (previous === undefined) delete process.env.ATRI_AGENT_CODE_MODE
    else process.env.ATRI_AGENT_CODE_MODE = previous
  }
})

test('memory tool has no target scope overrides and is absent in maintenance mode', () => {
  const previous = process.env.ATRI_AGENT_CODE_MODE
  try {
    for (const codeMode of ['false', 'true']) {
      process.env.ATRI_AGENT_CODE_MODE = codeMode
      const tools = []
      apply({ tools: { register(tool) { tools.push(tool) } } })
      const memory = tools.find(tool => tool.name === 'channel_memory')
      const voice = tools.find(tool => tool.name === 'send_voice')
      if (codeMode === 'true') {
        assert.equal(memory, undefined)
        assert.equal(voice, undefined)
      } else {
        assert.ok(voice)
        const speechSchema = JSON.stringify(voice)
        for (const phrase of ['说一下', '跟我说句话', '用你的声音说', '发段语音', '你说的是什么意思']) {
          assert.ok(speechSchema.includes(phrase), phrase)
        }
        for (const key of ['guild_id', 'channel_id', 'api_key', 'model', 'voice_id', 'url']) {
          assert.ok(!speechSchema.includes(`"${key}"`), key)
        }
        assert.ok(speechSchema.includes('"segments"'))
        assert.ok(speechSchema.includes('"style"'))
        assert.ok(speechSchema.includes('WITHIN one sentence'))
        assert.ok(!speechSchema.includes('"enum"'), 'speech style must not use a fixed emotion enum')
        assert.ok(memory)
        const schema = JSON.stringify(memory)
        for (const kind of ['episode', 'in_joke', 'impression']) assert.ok(schema.includes(`"${kind}"`))
        assert.ok(schema.includes('evidence_refs'))
        assert.ok(schema.includes('source_ref'))
        assert.ok(schema.includes('reinforce'))
        assert.ok(!schema.includes('"confidence"')) // support is computed by the host, not the model
        for (const key of ['guild_id', 'channel_id', 'user_id', 'message_id']) {
          assert.ok(!schema.includes(`"${key}"`), key)
        }
      }
    }
  } finally {
    if (previous === undefined) delete process.env.ATRI_AGENT_CODE_MODE
    else process.env.ATRI_AGENT_CODE_MODE = previous
  }
})
