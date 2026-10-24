/**
 * dsh-atbrains —— atbrains 共享长期记忆的 dsh 原生插件（Cordis）。
 *
 * 这个版本不再经过 MCP JSON-RPC，也不调用任何 Python 脚本：
 * 直接用 fetch 打 atbrains 的 /v1 HTTP 接口，读（召回）和写（归档）都在 Node 侧完成。
 *
 * 两个扩展点（签名取自 @deepseek-ai/dsh-agent 0.2.1-rc.2 的类型声明）：
 *   'agent/pre-step'(payload, next) waterfall —— 每轮第 1 步把相关记忆作为一条
 *       source.form='recall' 的 user 消息注入；失败静默降级。
 *   'agent/turn-stopping'(payload) serial —— 回合收尾时把整段会话（含工具调用/
 *       工具输出/推理）POST 到 /v1/memory/turn，落 brain_turns + brain_turn_messages。
 *
 * 召回与归档共用一个令牌；身份由服务端按令牌反查（`<主机>|<agent>`），
 * 客户端无法伪造 machine。
 */
import { randomUUID } from 'node:crypto'
import z from '@deepseek-ai/schemastery'
import { AtbrainsClient } from './client.js'
import { readSessionMessages, transcriptFromMessages } from './transcript.js'

export const name = 'atbrains-memory'

/** 只用事件与 logger，不依赖其它 Cordis 服务。 */
export const inject = []

export const Config = z.object({
  /** 服务地址（可写 http://host:8787 或 http://host:8787/mcp，都会归一化）。 */
  baseUrl: z.string().default('http://111.230.71.79:8787'),
  /** 令牌所在环境变量名；令牌本身不落配置文件。与 MCP 用同一个即可。 */
  tokenEnv: z.string().default('ATBRAINS_MCP_TOKEN'),
  /** 直接给令牌（优先级高于 tokenEnv；不建议写进文件）。 */
  token: z.string().default(''),
  /** 召回超时：记忆服务抖动不能拖慢整轮。 */
  timeoutMs: z.number().default(1200),
  /** 归档写超时（比召回宽松，写库要落 MariaDB + Redis）。 */
  writeTimeoutMs: z.number().default(4000),
  /** 注入文本上限（字符）。 */
  maxChars: z.number().default(1200),
  /** 召回条数上限。 */
  limit: z.number().default(5),
  /** 低于该分值的命中丢弃（服务端 vscore；无分值时保留）。 */
  minScore: z.number().default(0.25),
  /** 写入 brain_turns.platform 的值；dsh 自己的入口名，全小写。 */
  platform: z.string().default('dsh'),
  /** 写入 brain_turns.author；留空 = 由库侧按 (machine, platform) 规则回填。 */
  author: z.string().default(''),
  /** 自动归档开关（关掉就只剩召回）。 */
  archiveEnabled: z.boolean().default(true),
  /** 单条消息在转录层保留的字符上限（超长工具输出会被截断）。 */
  archiveMaxChars: z.number().default(16000),
  /** 每轮最多提交的消息条数（服务端还有 4000 条硬上限）。 */
  archiveMaxMessages: z.number().default(400),
  /** 注入行是否标注来源 agent（默认关，保持注入文本干净）。 */
  showAgentTag: z.boolean().default(false),
  /** 期望的 /v1 协议主版本；服务端 health.version 主版本不一致就停用（见 docs/protocol-v1.md）。 */
  protocolMajor: z.number().default(2),
})

const SOURCE_KIND = 'atbrains'
const RECALL_HEADER = '[atbrains 记忆] 以下是共享长期记忆里和本轮相关的条目（可能由别的 agent 写入，可能过时）：'
const RECALL_FOOTER = '如果这些与本轮无关，直接忽略即可，不要向用户复述。'

/** 与 Config 一致的兜底默认值：宿主没走 schema 校验时也不能出现 undefined 字段。 */
const DEFAULTS = {
  baseUrl: 'http://127.0.0.1:8787',
  tokenEnv: 'ATBRAINS_MCP_TOKEN',
  token: '',
  timeoutMs: 1200,
  writeTimeoutMs: 4000,
  maxChars: 1200,
  limit: 5,
  minScore: 0.25,
  platform: 'dsh',
  author: '',
  archiveEnabled: true,
  archiveMaxChars: 16000,
  archiveMaxMessages: 400,
  showAgentTag: false,
  protocolMajor: 2,
}

// ---------------------------------------------------------------- 文本处理

function textOf(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content
    .filter((b) => b && b.type === 'text' && typeof b.text === 'string')
    .map((b) => b.text)
    .join('')
}

function lastUserText(messages) {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i]
    if (m?.role !== 'user') continue
    if (m?.source?.kind === SOURCE_KIND) continue // 自己注入的那条不算用户提问
    const t = textOf(m.content).trim()
    if (t) return t.slice(0, 2000)
  }
  return ''
}

/** 与 dsh-mnemon 同款：user 角色 + 自有 source.kind + form='recall'。 */
export function memoryMessage(text) {
  return structuredClone({
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: SOURCE_KIND, form: 'recall' },
  })
}

/** 服务端已用 prefetch_block 排好序；这里只做截断与兜底渲染。 */
export function renderRecall(res, { maxChars = 1200, showAgentTag = false } = {}) {
  const items = Array.isArray(res?.items) ? res.items : []
  let body = String(res?.text || '').trim()
  if (!body && items.length) {
    const lines = []
    for (const m of items) {
      const content = String(m.preview || m.content || '').replace(/\s+/g, ' ').trim()
      if (!content) continue
      const tag = showAgentTag && m.agent ? `/${m.agent}` : ''
      lines.push(`- [${m.kind || 'note'}${tag}] ${content}`)
    }
    body = lines.join('\n')
  }
  if (!body) return ''
  if (!body.startsWith('[atbrains')) body = `${RECALL_HEADER}\n${body}`
  const full = body.includes('不要向用户复述') ? body : `${body}\n${RECALL_FOOTER}`
  return full.length <= maxChars ? full : `${full.slice(0, maxChars)}…`
}

function scoreOf(item) {
  const v = item?.vscore
  return typeof v === 'number' ? v : null
}

function sessionKeyOf(session, fallback) {
  const id = session?.id ?? fallback
  return String(id ?? 'unknown')
}

// ---------------------------------------------------------------- 运行时

/**
 * 组装运行时。测试可以直接拿 runtime.recall / runtime.archive 驱动，
 * 也可以注入 client 替换网络层。
 */
export function createRuntime(ctx, rawConfig, { clientFactory } = {}) {
  const logger = ctx?.logger
  const config = { ...DEFAULTS, ...(rawConfig || {}) }
  const token = String(config.token || process.env[config.tokenEnv] || '').trim()
  if (!token && !clientFactory) {
    logger?.error?.(`[atbrains] 未找到令牌：环境变量 ${config?.tokenEnv} 为空，插件不工作`)
    return { enabled: false }
  }
  const client = clientFactory
    ? clientFactory(config)
    : new AtbrainsClient({
        baseUrl: config.baseUrl,
        token,
        timeoutMs: config.timeoutMs,
        writeTimeoutMs: config.writeTimeoutMs,
      })

  // 会话 → 追加到模型面的消息（只在 surface API 不可用时兜底）
  const buffers = new Map()
  // 会话 → 串行队列，避免两次 turn-stopping 交错写游标
  const queues = new Map()
  // 会话 → 上次提交的条数，纯优化（服务端游标才是权威）
  const lastCount = new Map()

  // /v1 协议主版本校验：只在进程内查一次；不一致就停用，避免“字段悄悄变了还在写”。
  const expectedMajor = String(config.protocolMajor ?? 2)
  let protocolProbe = null
  function protocolOk() {
    if (!protocolProbe) {
      protocolProbe = (async () => {
        try {
          const h = await client.health()
          const major = String(h?.version || '').split('.')[0]
          if (major && major !== expectedMajor) {
            logger?.error?.(
              `[atbrains] 服务端协议 v${h.version} 与插件期望 v${expectedMajor}.x 不匹配，已停用记忆（升级插件或改 protocolMajor）`,
            )
            return false
          }
          return true
        } catch {
          // 探活失败不判死：交给真实请求按超时/失败降级，避免服务端一抖就永久停用
          return true
        }
      })()
    }
    return protocolProbe
  }

  function pushBuffered(key, message) {
    const arr = buffers.get(key) || []
    arr.push(message)
    if (arr.length > 400) arr.splice(0, arr.length - 400)
    buffers.set(key, arr)
  }

  /** 首选：直接从会话的模型可见面读全量对话；读不到再退回缓冲区。 */
  function collectMessages(session, key) {
    const fromSurface = readSessionMessages(session)
    if (fromSurface.length) return fromSurface
    return buffers.get(key) || []
  }

  async function archive(payload) {
    if (!config.archiveEnabled) return null
    if (!(await protocolOk())) return null
    const session = payload?.agent?.session
    const key = sessionKeyOf(session, `turn-${payload?.turn ?? ''}`)
    const raw = collectMessages(session, key)
    if (!raw.length) return null
    const t = transcriptFromMessages(raw, {
      maxChars: config.archiveMaxChars,
      maxMessages: config.archiveMaxMessages,
    })
    if (!t.user) return null
    if ((lastCount.get(key) ?? -1) === t.records.length) return null
    const sessionId = String(session?.id ?? key)
    const prev = queues.get(key) || Promise.resolve()
    const run = prev.then(async () => {
      try {
        const res = await client.turn({
          session_id: sessionId,
          user: t.user,
          assistant: t.assistant,
          platform: config.platform,
          author: config.author,
          messages: t.records,
        })
        lastCount.set(key, t.records.length)
        logger?.debug?.(`[atbrains] 已归档 turn=${payload?.turn} id=${res?.id} msgs=${t.records.length}`)
        return res
      } catch (error) {
        logger?.warn?.(`[atbrains] 归档失败，已跳过：${String(error?.message || error)}`)
        return null
      }
    })
    queues.set(key, run)
    return run
  }

  async function recall(payload, decision) {
    if (decision.kind !== 'enter') return decision
    if (payload?.signal?.aborted) return decision
    if (payload?.step !== 1) return decision
    const messages = Array.isArray(decision.messages) ? decision.messages : []
    if (!messages.length) return decision
    if (messages.some((m) => m?.source?.kind === SOURCE_KIND)) return decision
    const query = lastUserText(messages)
    if (!query) return decision
    if (!(await protocolOk())) return decision
    try {
      const res = await client.recall(query, {
        sessionId: payload?.agent?.session?.id,
        limit: config.limit,
      })
      const items = (Array.isArray(res?.items) ? res.items : []).filter((m) => {
        const s = scoreOf(m)
        return s === null || s >= config.minScore
      })
      const text = renderRecall({ ...res, items }, config)
      if (!text) return decision
      return { kind: 'enter', messages: [...messages, memoryMessage(text)] }
    } catch (error) {
      logger?.warn?.(`[atbrains] 召回失败，已跳过：${String(error?.message || error)}`)
      return decision
    }
  }

  /** session/event 兜底：只在拿不到 surface 时才用得上。 */
  function observeEvent(session, event) {
    const type = String(event?.type || '')
    if (type !== 'user/message' && type !== 'assistant/message' && type !== 'tool/result') return
    const op = event?.surfaceOp
    if (op && op !== 'append') return
    const data = event?.data
    if (!data || typeof data !== 'object') return
    const key = sessionKeyOf(session, 'unknown')
    if (type === 'tool/result') {
      pushBuffered(key, {
        role: 'tool',
        content: data.content,
        toolCallId: data.toolCallId ?? data.callId,
      })
      return
    }
    pushBuffered(key, {
      role: type === 'user/message' ? 'user' : 'assistant',
      content: data.content,
    })
  }

  return {
    enabled: true,
    client,
    recall: (payload, decision) => recall(payload, decision),
    archive,
    observeEvent,
    _buffers: buffers,
    _lastCount: lastCount,
  }
}

// ---------------------------------------------------------------- 插件入口

export function apply(ctx, rawConfig) {
  const runtime = createRuntime(ctx, rawConfig)
  if (!runtime.enabled) return
  const config = { ...DEFAULTS, ...(rawConfig || {}) }
  ctx.logger?.info?.(
    `[atbrains] 已启用 → ${runtime.client.baseUrl}（召回 + ${config.archiveEnabled ? '自动归档' : '不归档'}）`,
  )

  ctx.effect(() => {
    const offPre = ctx.on('agent/pre-step', async (payload, next) => {
      const decision = await next()
      return runtime.recall(payload, decision)
    })
    const offEvent = ctx.on('session/event', (session, event) => runtime.observeEvent(session, event))
    const offStop = ctx.on('agent/turn-stopping', async (payload) => {
      await runtime.archive(payload)
    })
    return () => {
      offPre?.()
      offEvent?.()
      offStop?.()
    }
  }, 'atbrains-memory: recall + archive')
}

export default { name, inject, Config, apply }
