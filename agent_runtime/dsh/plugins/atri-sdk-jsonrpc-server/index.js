import Schema from '@deepseek-ai/schemastery'
import { JsonRpcLineTransport } from '@deepseek-ai/dsh-sdk-protocol'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import * as Persona from '@deepseek-ai/dsh-persona'
import { SessionId } from '@deepseek-ai/dsh-session'
import { HarnessSdkJsonRpcServer } from '@deepseek-ai/dsh-sdk-jsonrpc-server'


/**
 * The upstream SDK demo always calls agents.create() for the first prompt seen
 * by a newly started process. ATRI uses deterministic channel session ids, so
 * a process restart must resume an existing JSONL log instead of attempting to
 * materialize the same id again.
 */
export class AtriResumableHarnessSdkServer extends HarnessSdkJsonRpcServer {
  constructor(...args) {
    super(...args)
    this.sessionPersonas = new Map()
    this.sessionRuntimeContexts = new Map()
  }

  async createSession(sessionId) {
    const agentOptions = {
      provider: this.provider,
      model: this.model,
      ...(this.maxTokens === undefined ? {} : { maxTokens: this.maxTokens }),
    }
    const persona = this.sessionPersonas.get(sessionId)
    const setup = persona === undefined
      ? undefined
      : async (agentCtx) => {
          agentCtx.systemPrompt.variable(
            'atri_live_turn_context',
            () => this.sessionRuntimeContexts.get(sessionId) ?? '',
          )
          const handle = agentCtx.plugin(Persona, {
            text: `${persona}\n\n{{atri_live_turn_context}}`,
            complete: false,
            includeRuntimeContext: true,
          })
          await handle
        }
    const persistence = this.ctx.get('sessionPersistence')
    let handle
    if (persistence !== undefined) {
      const headers = await persistence.list()
      const alreadyExists = headers.some((header) => String(header.id) === sessionId)
      if (alreadyExists) {
        handle = await this.ctx.agents.resume({
          resumeSessionId: SessionId(sessionId),
          agentOptions,
          ...(setup === undefined ? {} : { setup }),
        })
      }
    }
    if (handle === undefined) {
      handle = await this.ctx.agents.create({
        sessionId: SessionId(sessionId),
        meta: { cwd: this.cwd },
        agentOptions,
        ...(setup === undefined ? {} : { setup }),
      })
    }
    const record = { handle, persona }
    this.sessions.set(sessionId, record)
    return record
  }

  async handleRequest(method, params) {
    if (method === 'session/context') {
      const sessionId = params?.sessionId
      const context = params?.context
      if (typeof sessionId !== 'string' || sessionId.length === 0) {
        throw new TypeError('session/context requires a non-empty sessionId')
      }
      if (
        typeof context !== 'string'
        || context.length === 0
        || context.length > 50_000
      ) {
        throw new TypeError('session/context requires 1 to 50000 characters')
      }
      const changed = this.sessionRuntimeContexts.get(sessionId) !== context
      this.sessionRuntimeContexts.set(sessionId, context)
      return { sessionId, configured: true, changed }
    }
    if (method === 'session/persona') {
      const sessionId = params?.sessionId
      const persona = params?.persona
      if (typeof sessionId !== 'string' || sessionId.length === 0) {
        throw new TypeError('session/persona requires a non-empty sessionId')
      }
      if (
        typeof persona !== 'string'
        || persona.length === 0
        || persona.length > 250_000
      ) {
        throw new TypeError('session/persona requires 1 to 250000 characters')
      }
      const previous = this.sessionPersonas.get(sessionId)
      const record = this.sessions.get(sessionId)
      if (previous === persona && (record === undefined || record.persona === persona)) {
        return { sessionId, configured: true, changed: false, recycled: false }
      }

      this.sessionPersonas.set(sessionId, persona)
      const pending = this.sessionCreations.get(sessionId)
      if (pending !== undefined) await pending
      const liveRecord = this.sessions.get(sessionId)
      let recycled = false
      if (liveRecord !== undefined && liveRecord.persona !== persona) {
        await liveRecord.handle.agent.whenIdle()
        this.sessions.delete(sessionId)
        await liveRecord.handle.dispose()
        recycled = true
      }
      return {
        sessionId,
        configured: true,
        changed: previous !== persona,
        recycled,
      }
    }
    if (method === 'session/ensure') {
      const sessionId = params?.sessionId
      if (typeof sessionId !== 'string' || sessionId.length === 0) {
        throw new TypeError('session/ensure requires a non-empty sessionId')
      }
      await this.getOrCreateSession(sessionId)
      return { sessionId }
    }
    if (method === 'session/inject') {
      const sessionId = params?.sessionId
      const contentBlocks = params?.contentBlocks
      if (typeof sessionId !== 'string' || sessionId.length === 0) {
        throw new TypeError('session/inject requires a non-empty sessionId')
      }
      if (
        !Array.isArray(contentBlocks)
        || contentBlocks.length === 0
        || contentBlocks.some((block) => (
          block === null
          || typeof block !== 'object'
          || block.type !== 'text'
          || typeof block.text !== 'string'
          || block.text.length === 0
        ))
      ) {
        throw new TypeError('session/inject requires non-empty text contentBlocks')
      }
      const rec = await this.getOrCreateSession(sessionId)
      const message = createUserMessage({
        content: contentBlocks,
        source: {
          kind: 'plugin',
          plugin: 'atri-passive-discord',
          form: 'recall',
        },
      })
      const event = rec.handle.agent.session.append('user/message', message, {
        surfaceOp: 'append',
      })
      // Passive channel speech has no turn/end checkpoint, so explicitly wait
      // for the session store's durability barrier before acknowledging it.
      await this.ctx.sessions.flush(rec.handle.agent.session)
      return {
        sessionId,
        messageId: message.id,
        eventSeq: event.seq,
        recorded: true,
      }
    }
    if (method === 'session/compact') {
      const sessionId = params?.sessionId
      if (typeof sessionId !== 'string' || sessionId.length === 0) {
        throw new TypeError('session/compact requires a non-empty sessionId')
      }
      const compaction = this.ctx.get('compaction')
      if (compaction === undefined || typeof compaction.compactNow !== 'function') {
        throw new Error('DSH compaction service is unavailable')
      }
      const rec = await this.getOrCreateSession(sessionId)
      const controller = new AbortController()
      const result = await compaction.compactNow(rec.handle.agent, controller.signal)
      if (result === null) {
        return {
          sessionId,
          compacted: false,
          shadowedItems: 0,
          shadowedTokens: 0,
        }
      }
      return {
        sessionId,
        compacted: true,
        shadowedItems: result.shadowedSeqs.length,
        shadowedTokens: result.shadowedTokenCount,
        summarySeq: result.summarySeq,
      }
    }
    if (method === 'session/cancel') {
      const sessionId = params?.sessionId
      if (typeof sessionId !== 'string' || sessionId.length === 0) {
        throw new TypeError('session/cancel requires a non-empty sessionId')
      }
      const pending = this.sessionCreations.get(sessionId)
      if (pending !== undefined) await pending
      const record = this.sessions.get(sessionId)
      if (record === undefined) return { sessionId, cancelled: false }
      this.sessions.delete(sessionId)
      await record.handle.dispose()
      return { sessionId, cancelled: true }
    }
    return super.handleRequest(method, params)
  }
}


export const name = 'atri-sdk-jsonrpc-server'
export const inject = ['agents', 'sessions']
export const Config = Schema.object({
  maxTokensAsSuccess: Schema.boolean().default(false),
})


export function apply(ctx, config) {
  const input = config.input ?? process.stdin
  const output = config.output ?? process.stdout
  const exit = config.exit ?? ((code) => process.exit(code))
  const transport = new JsonRpcLineTransport(input, output)
  const server = new AtriResumableHarnessSdkServer(ctx, transport, {
    maxTokensAsSuccess: config.maxTokensAsSuccess,
  })
  const rootFiber = ctx.root.fiber
  let exitTask

  const disposeAndExit = () => {
    exitTask ??= (async () => {
      await Promise.allSettled([Promise.resolve().then(() => transport.flush())])
      await Promise.allSettled([Promise.resolve().then(() => rootFiber.dispose())])
      exit(0)
    })()
    return exitTask
  }

  transport.onRequest(async (method, params) => {
    const result = await server.handleRequest(method, params)
    if (method === 'shutdown') {
      setImmediate(() => {
        void disposeAndExit()
      })
    }
    return result
  })

  ctx.effect(() => {
    transport.start()
    return async () => {
      await server.shutdown()
      transport.close()
    }
  }, 'atri-jsonrpc.serve')
}
