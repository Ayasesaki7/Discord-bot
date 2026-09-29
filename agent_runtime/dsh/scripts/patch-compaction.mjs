import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'

// rc.6 has no public idle-compaction range hook. Patch only that range selector;
// preserve its maintenance lock, tool pairing, transaction, abort and flush.
const upstreamHash = '144202a0f150b9b7984842d6316808aefcbdf14e7a890805cb0819f4cc69740f'
const before = 'const range = selectCompactableRange(agent.session, this.ctx.tokenMeter.measure(agent.session), 0);'
const after = `// ATRI_MANUAL_RETAIN_POLICY_V1
					const target = conversationTarget(agent);
					const policy = target === void 0 ? this.config : resolveTargetPolicy(this.config, target);
					let retainTokens = policy.retainTokens;
					if (retainTokens === void 0) {
						if (target === void 0) throw new Error("manual retention requires a model target");
						const info = await this.ctx.llm.resolveModelInfo(target.provider, target.model, operationSignal);
						if (info.context === void 0) throw new Error("manual retention requires context capacity");
						retainTokens = Math.floor(info.context.contextWindow * policy.retainRatio);
					}
					operationSignal.throwIfAborted();
					const range = selectCompactableRange(agent.session, this.ctx.tokenMeter.measure(agent.session), retainTokens);
					// Re-summarizing the lone checkpoint cannot free an older conversation span.
					if (range !== null && range.start === range.end && agent.session.events[range.start]?.data?.source?.plugin === "compact") return null;`

export const compactionPath = fileURLToPath(import.meta.resolve('@deepseek-ai/dsh-compaction-basic'))

export function patchedSource(source) {
  const original = source.includes(after) ? source.replace(after, before) : source
  if (createHash('sha256').update(original).digest('hex') !== upstreamHash) {
    throw new Error('Unsupported compaction-basic source: review the retention patch before upgrading DSH')
  }
  if (original.split(before).length !== 2) throw new Error('Manual compaction patch target is not unique')
  return original.replace(before, after)
}

export function verifyCompactionPatch() {
  const source = readFileSync(compactionPath, 'utf8')
  if (patchedSource(source) !== source) {
    throw new Error('Missing manual retention patch: run npm run patch:compaction in agent_runtime/dsh')
  }
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  const source = readFileSync(compactionPath, 'utf8')
  const patched = patchedSource(source)
  if (source !== patched) writeFileSync(compactionPath, patched)
  verifyCompactionPatch()
  console.log('DSH manual retention patch verified (rc.6)')
}
