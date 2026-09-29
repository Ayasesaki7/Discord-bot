import { TokenMeter } from '@deepseek-ai/dsh-token-meter'
import { canonicalHeader } from '@deepseek-ai/dsh-session'
import { headerTokens, messageTokens } from '../atri-compaction/tokens.js'

const routeKey = header => JSON.stringify([header?.config?.provider, header?.config?.model])

// Keep the native service registration; replace only the pressure/retention
// measure. Historical shadow-price claims used chars/4 and cannot be mixed with
// the new prices. Fold actual effective surface nodes instead of those claims.
export default class AtriTokenMeter extends TokenMeter {
  calibratedStates = new WeakMap()

  estimateMessage(message) { return messageTokens(message) }

  measure(session, requestHeader) {
    this.calibratedStates ??= new WeakMap()
    let state = this.calibratedStates.get(session)
    if (!state) {
      state = { consumed: 0, nodes: [], surface: 0, header: undefined, stepSurface: undefined, ratios: [], route: '' }
      this.calibratedStates.set(session, state)
    }
    const events = session.events
    for (; state.consumed < events.length; state.consumed++) {
      const event = events[state.consumed]
      const data = event.data || {}
      if (event.type === 'request/header') {
        state.header = canonicalHeader(data.header)
        const route = routeKey(state.header)
        if (state.route !== route) { state.route = route; state.ratios = [] }
      }
      if (event.type === 'step/start') state.stepSurface = state.surface
      if (event.type === 'assistant/message' && state.stepSurface !== undefined) {
        const usage = data.usage || {}
        const input = ['inputTokens', 'cacheReadTokens', 'cacheWriteTokens'].reduce((n, key) => n + Math.max(0, Number(usage[key]) || 0), 0)
        const base = headerTokens(state.header) + state.stepSurface
        if (input > 0 && base > 0) {
          // Recent high-water calibration avoids cache-route oscillation. Do
          // not carry a fixed +70k usage offset across a successful compaction.
          state.ratios.push(Math.max(1, Math.min(4, input / base)))
          state.ratios = state.ratios.slice(-4)
        }
      }
      if (event.type === 'step/end') state.stepSurface = undefined
      const op = event.surfaceOp
      if (op === undefined) continue
      const node = { seq: event.seq, tokens: messageTokens(session.deriveEventMessage(event)) }
      if (op === 'append') {
        state.nodes.push(node)
        state.surface += node.tokens
      } else {
        const start = state.nodes.findIndex(n => n.seq === op.start)
        const end = state.nodes.findIndex(n => n.seq === op.end)
        if (start < 0 || end < start) throw new Error('ATRI meter: invalid surface replacement')
        const removed = state.nodes.splice(start, end - start + 1, node)
        state.surface += node.tokens - removed.reduce((n, item) => n + item.tokens, 0)
      }
    }
    const header = requestHeader === undefined ? state.header : canonicalHeader(requestHeader)
    const sameRoute = routeKey(header) === state.route
    const calibration = sameRoute ? Math.max(1, ...state.ratios) : 1
    const nodes = state.nodes.map(node => ({ seq: node.seq, tokens: Math.ceil(node.tokens * calibration) }))
    const surfaceTokens = nodes.reduce((n, node) => n + node.tokens, 0)
    const envelopeTokens = Math.ceil(headerTokens(header) * calibration)
    const totalTokens = envelopeTokens + surfaceTokens
    return { logRevision: events.length, baseline: { kind: 'estimated', tokens: totalTokens },
      surfaceDeltaTokens: 0, totalTokens, surfaceTokens, nodes,
      envelopeTokens, calibration, estimator: 'atri-multilingual-v1' }
  }
}
