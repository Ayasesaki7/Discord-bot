import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import BasicCompactionEngine from '../plugins/atri-compaction/index.js'
import { patchedSource, compactionPath } from '../scripts/patch-compaction.mjs'
import { Session } from '@deepseek-ai/dsh-session'
import { TokenMeter } from '@deepseek-ai/dsh-token-meter'
import { createUserMessage } from '@deepseek-ai/dsh-llm'

function harness({ retainTokens = 28_000, count = 12, fail, busy = false } = {}) {
  const session = Session.create('retention-test')
  for (let i = 0; i < count; i++) {
    session.append('user/message', createUserMessage({ content: [{ type: 'text', text: `message ${i}: ` + 'x'.repeat(16_000) }], source: { kind: 'plugin', plugin: 'test' } }), { surfaceOp: 'append' })
  }
  // Exercise the real meter, session, selector and transaction; only the LLM,
  // durability I/O and idle-agent admission are test doubles. No API calls.
  const meter = Object.assign(Object.create(TokenMeter.prototype), { states: new WeakMap() })
  const engine = Object.create(BasicCompactionEngine.prototype)
  let flushes = 0
  Object.defineProperty(engine, 'ctx', { value: {
    tokenMeter: meter, sessions: { async flush() { flushes++ } },
    llm: { async resolveModelInfo() { return { context: { contextWindow: 140_000 } } } },
    get() { return undefined },
  } })
  engine.config = { retainTokens, modelPolicies: [], thresholdRatio: 0.1, compactionRetries: 1 }
  engine.summarize = async () => {
    if (fail) throw new Error('simulated summary failure')
    return { summary: [{ type: 'text', text: 'earlier facts' }], provider: 'test', model: 'test' }
  }
  const agentSignal = new AbortController().signal
  const agent = {
    session, options: { provider: 'test', model: 'test' },
    runMaintenance(callback) {
      if (busy) throw new Error('busy')
      return callback(agentSignal)
    },
  }
  return { session, engine, agent, meter, flushes: () => flushes }
}

test('manual retains at least 28k priced tail, flushes once and is repeatable', async () => {
  const h = harness()
  const before = h.meter.measure(h.session)
  const result = await h.engine.compactNow(h.agent, new AbortController().signal)
  assert.ok(result)
  const retained = before.nodes.filter(node => !result.shadowedSeqs.includes(node.seq))
  assert.ok(retained.reduce((n, node) => n + node.tokens, 0) >= 28_000)
  assert.equal(retained.length, 7)
  assert.equal(h.flushes(), 1)
  assert.deepEqual(h.session.surface.nodes.slice(1), retained.map(node => node.seq))
  // A second request cannot force the retained 28k through the summarizer.
  const second = await h.engine.compactNow(h.agent, new AbortController().signal)
  assert.equal(second, null)
  assert.equal(h.flushes(), 1)
  assert.deepEqual(h.session.surface.nodes.slice(1), retained.map(node => node.seq))
})

test('short history is a no-op instead of erasing everything but one message', async () => {
  const h = harness({ count: 3 })
  const before = h.session.events
  assert.equal(await h.engine.compactNow(h.agent, new AbortController().signal), null)
  assert.deepEqual(h.session.events, before)
  assert.equal(h.flushes(), 0)
})

test('failed summary preserves every surface node and closes the maintenance transaction', async () => {
  const h = harness({ fail: true })
  const before = [...h.session.surface.nodes]
  await assert.rejects(h.engine.compactNow(h.agent, new AbortController().signal), error => error.code === 'summary')
  assert.deepEqual(h.session.surface.nodes, before)
  assert.equal(h.session.events.at(-1).type, 'compaction/end')
  assert.equal(h.flushes(), 1)
})

test('cancelled and busy requests cannot mutate the session', async () => {
  const h = harness({ busy: true })
  const before = h.session.events
  assert.throws(() => h.engine.compactNow(h.agent, new AbortController().signal), error => error.code === 'busy')
  const abort = new AbortController()
  abort.abort()
  assert.throws(() => h.engine.compactNow(h.agent, abort.signal))
  assert.deepEqual(h.session.events, before)
})

test('retention boundary keeps a complete assistant tool-call/result pair', async () => {
  const h = harness({ retainTokens: 1000, count: 3 })
  h.session.append('step/start', {})
  const call = h.session.append('assistant/message', { message: { role: 'assistant', content: [{ type: 'tool-call', name: 'test', toolCallId: 'test-call', arguments: '{}' }] } }, { surfaceOp: 'append' })
  const result = h.session.append('tool/result', { name: 'test', message: { role: 'user', content: [{ type: 'tool-result', toolCallId: 'test-call', content: [{ type: 'text', text: 'result '.repeat(700) }] }] } }, { surfaceOp: 'append' })
  h.session.append('step/end', {})
  const compacted = await h.engine.compactNow(h.agent, new AbortController().signal)
  assert.ok(compacted)
  assert.deepEqual(h.session.surface.nodes.slice(1), [call.seq, result.seq])
})

test('manual respects an exact model retention override', async () => {
  const h = harness()
  h.engine.config.modelPolicies = [{ provider: 'test', model: 'test', retainTokens: 12_000 }]
  const result = await h.engine.compactNow(h.agent, new AbortController().signal)
  assert.ok(result)
  assert.equal(h.session.surface.nodes.length, 4) // checkpoint + three 4k nodes
})

test('ratio-based policies also retain a tail', async () => {
  const h = harness()
  delete h.engine.config.retainTokens
  h.engine.config.retainRatio = 0.2
  const result = await h.engine.compactNow(h.agent, new AbortController().signal)
  assert.ok(result)
  assert.equal(h.session.surface.nodes.length, 8)
})

test('patch is idempotent and rejects unreviewed dependency upgrades', () => {
  const source = readFileSync(compactionPath, 'utf8')
  assert.equal(patchedSource(source), source)
  assert.throws(() => patchedSource(source + '\n// unreviewed upgrade'))
})
