import { verifyCompactionPatch } from '../../scripts/patch-compaction.mjs'

// Read-only startup guard: npm ci/postinstall applies the version-pinned patch.
// Never silently revert to the upstream zero-tail manual compaction behavior.
verifyCompactionPatch()
const BasicCompactionEngine = (await import('@deepseek-ai/dsh-compaction-basic')).default

export default class AtriCompactionEngine extends BasicCompactionEngine {
  targetTokens() {
    const window = Number(process.env.ATRI_DSH_CONTEXT_WINDOW || 140000)
    return Math.floor((Number.isFinite(window) && window > 0 ? window : 140000) * 0.5)
  }

  async summarize(input, agent, signal) {
    const measured = this.ctx.tokenMeter.measure(agent.session)
    const pricedHead = input.messages.reduce((n, message) => n + this.ctx.tokenMeter.estimateMessage(message), 0) * (measured.calibration || 1)
    const tailAndEnvelope = Math.max(0, measured.totalTokens - pricedHead)
    // Bound the summary request by the post-compaction headroom, while keeping
    // the configured 30k as a maximum, never a mandatory summary size.
    const budget = Math.max(512, Math.floor(this.targetTokens() - tailAndEnvelope - 512))
    const facade = Object.create(this)
    facade.config = { ...this.config, maxTokens: Math.min(this.config.maxTokens || 30000, budget),
      modelPolicies: (this.config.modelPolicies || []).map(p => ({ ...p, maxTokens: Math.min(p.maxTokens || this.config.maxTokens || 30000, budget) })) }
    return BasicCompactionEngine.prototype.summarize.call(facade, input, agent, signal)
  }

  reportBudget(agent, result) {
    if (!result) return result
    const measured = this.ctx.tokenMeter.measure(agent.session)
    const target = this.targetTokens()
    const budget = { estimatedInputTokens: measured.totalTokens, targetTokens: target,
      withinTarget: measured.totalTokens <= target, estimator: measured.estimator || 'native' }
    const text = `ATRI compaction budget: estimated_input=${budget.estimatedInputTokens}, target=${target}, within_target=${budget.withinTarget}`
    if (budget.withinTarget) this.ctx.logger?.info(text)
    else this.ctx.logger?.warn(text + '; inspect retained whole messages/tool pairs and fixed envelope; no blind retry')
    return { ...result, budget }
  }

  async compactRegion(start, end, agent, signal) {
    if (start === end && agent.session.events[start]?.data?.source?.plugin === 'compact') return null
    return this.reportBudget(agent, await super.compactRegion(start, end, agent, signal))
  }

  compactNow(agent, signal, sourceCommandId) {
    // Preserve the native synchronous busy/abort errors and maintenance lock.
    return super.compactNow(agent, signal, sourceCommandId).then(result => this.reportBudget(agent, result))
  }
}
