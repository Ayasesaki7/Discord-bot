import assert from 'node:assert/strict'
import test from 'node:test'
import { Session } from '@deepseek-ai/dsh-session'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import Engine from '../plugins/atri-compaction/index.js'
import Meter from '../plugins/atri-token-meter/index.js'
import { textTokens, messageTokens } from '../plugins/atri-compaction/tokens.js'

const meter = () => Object.create(Meter.prototype)
const user = (s, text, op = 'append') => s.append('user/message', createUserMessage({content: [{type: 'text', text}], source: {kind: 'plugin', plugin: 'test'}}), {surfaceOp: op, ...(op === 'append' ? {} : {sourceEventSeqs: [op.start]})})
const header = (s, model = 'test') => s.append('request/header', {header: {config: {provider: 'test', model}, system: '规则'.repeat(2000), tools: []}})
function harness(session) {
  const m = meter()
  const engine = Object.create(Engine.prototype)
  let calls = 0
  Object.defineProperty(engine, 'ctx', {value: { tokenMeter: m, sessions: {async flush() {}},
    llm: {async resolveModelInfo() {return {context: {contextWindow: 140000}}}}, get() {} }})
  engine.config = {retainTokens: 28000, maxTokens: 30000, thresholdRatio: .78, compactionRetries: 0,
    modelPolicies: [], summarizationProvider: '', summarizationModel: ''}
  engine.summarize = async () => { calls++; return {summary: [{type: 'text', text: '摘要'.repeat(500)}], provider: 'test', model: 'test'} }
  const agent = {session, options: {provider: 'test', model: 'test'}, runMaintenance(fn) {return fn(new AbortController().signal)}}
  return {engine, agent, meter: m, calls: () => calls}
}

test('multilingual prices Chinese conservatively without treating image base64 as prose', () => {
  assert.equal(textTokens('中'.repeat(28000)), 33600)
  assert.equal(textTokens('a'.repeat(28000)), 9334)
  assert.equal(textTokens('😀'), 3)
  assert.ok(messageTokens({content: [{type: 'image', data: 'x'.repeat(1000000)}]}) < 3000)
})

test('provider calibration shrinks with actual surface; old shadow price cannot leave a fixed 70k residual', () => {
  const s = Session.create('meter-calibration')
  header(s)
  const a = user(s, '历史'.repeat(20000))
  const b = user(s, '近况'.repeat(1000))
  const m = meter()
  const before = m.measure(s).totalTokens
  s.append('step/start', {})
  s.append('assistant/message', {message: {role: 'assistant', content: [{type: 'text', text: 'ok'}]}, usage: {inputTokens: before, cacheReadTokens: before, outputTokens: 1}}, {surfaceOp: 'append'})
  s.append('step/end', {})
  assert.equal(m.measure(s).calibration, 2)
  user(s, '旧事摘要', {op: 'replace', start: a.seq, end: a.seq})
  const after = m.measure(s)
  assert.ok(after.totalTokens < 20000)
  assert.deepEqual(after, m.measure(s))
  assert.ok(after.nodes.some(n => n.seq === b.seq))
  header(s, 'new-provider-model')
  assert.equal(m.measure(s).calibration, 1)
})

test('split Chinese recovered history retains ~28k, compacts well below 90k and repeat manual is no-op', async () => {
  const s = Session.create('chinese-recovery')
  header(s)
  for (let i = 0; i < 29; i++) user(s, '中'.repeat(4000))
  const h = harness(s)
  const before = h.meter.measure(s)
  const result = await h.engine.compactNow(h.agent, new AbortController().signal)
  const retained = before.nodes.filter(n => !result.shadowedSeqs.includes(n.seq)).reduce((n, p) => n + p.tokens, 0)
  assert.ok(retained >= 28000 && retained < 33000)
  assert.ok(result.budget.withinTarget)
  assert.ok(result.budget.estimatedInputTokens < 40000)
  assert.equal(await h.engine.compactNow(h.agent, new AbortController().signal), null)
  assert.equal(h.calls(), 1)
})

test('automatic pressure compaction also retains the priced tail and runs only once', async () => {
  const s = Session.create('chinese-auto')
  header(s)
  for (let i = 0; i < 29; i++) user(s, '中'.repeat(4000))
  const h = harness(s)
  s.append('turn/start', {turn: 'budget-turn'})
  const result = await h.engine.compactIfNeeded(h.agent, 'pressure', new AbortController().signal)
  assert.ok(result.budget.withinTarget)
  assert.equal(h.calls(), 1)
})

test('summary output budget accounts for remaining tail plus system/tool envelope', async () => {
  const s = Session.create('output-budget')
  header(s)
  const old = user(s, 'old')
  user(s, '中'.repeat(36000))
  const h = harness(s)
  delete h.engine.summarize
  let outputLimit
  h.engine.ctx.llm.stream = async function* (options) {
    outputLimit = options.maxTokens
    throw new Error('stop before network')
  }
  await assert.rejects(h.engine.summarize({messages: [s.deriveEventMessage(old)]}, h.agent), /stop before network/)
  assert.ok(outputLimit < 25000 && outputLimit > 512)
})

test('oversized irreducible tail is reported, not called successfully within budget', async () => {
  const s = Session.create('large-boundary')
  header(s)
  user(s, 'old'.repeat(10000))
  user(s, '中'.repeat(60000))
  const h = harness(s)
  const result = await h.engine.compactNow(h.agent, new AbortController().signal)
  assert.equal(result.budget.withinTarget, false)
  assert.equal(h.calls(), 1)
  const checkpoint = s.surface.nodes[0]
  assert.equal(await h.engine.compactRegion(checkpoint, checkpoint, h.agent), null)
  assert.equal(h.calls(), 1)
})
