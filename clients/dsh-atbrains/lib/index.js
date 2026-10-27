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
 * 召回与归档共用一个令牌；服务端按令牌反查 machine/agent（`<主机>|<agent>`），
 * 客户端不能伪造 machine。插件另存一份本机身份（machine/os/author）：
 *   · 第一次安装或升级后发现缺值 → 下一轮 pre-step 注入 source.form='setup' 提示；
 *   · 模型可用 atbrains_identity 工具保存，用户可用 /atbrains 命令重设；
 *   · 状态写在 $DSH_HOME/atbrains/identity.json，升级覆盖 lib/ 不会丢。
 */
import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import z from '@deepseek-ai/schemastery'
import { AtbrainsClient } from './client.js'
import { readSessionMessages, transcriptFromMessages, isSyntheticUser } from './transcript.js'
import {
  createIdentityController,
  parseIdentityInput,
  renderIdentity,
  setupNotice,
  IDENTITY_USAGE,
} from './identity.js'

export const name = 'atbrains-memory'

/** 只用事件与 logger，不依赖其它 Cordis 服务；commands/tools 走可选注入。 */
export const inject = []

/** 当前插件版本（用来识别「第一次安装 / 刚刚升级」）。 */
const PLUGIN_VERSION = (() => {
  try {
    return String(JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version || '0.0.0')
  } catch {
    return '0.0.0'
  }
})()

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
  /** 本机机器名（首次安装/升级时作为身份初始值；空 = 用 hostname 自动检测）。 */
  machine: z.string().default(''),
  /** 本机操作系统（空 = 自动检测；Windows 会按 build 号区分 10/11）。 */
  os: z.string().default(''),
  /** 身份状态文件；空 = $DSH_HOME/atbrains/identity.json（默认 ~/.dsh/...）。 */
  identityFile: z.string().default(''),
  /** 首次对话是否注入身份初始化提示（关掉只保留 /atbrains 命令与工具）。 */
  onboarding: z.boolean().default(true),
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
  machine: '',
  os: '',
  identityFile: '',
  onboarding: true,
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
    if (isSyntheticUser(m)) continue // 自己注入的召回 / 宿主的运行时上下文与 skill 清单都不算用户提问
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

/** 身份初始化提示：同一 source.kind，但 form='setup'，不参与召回去重也不入库。 */
export function setupMessage(text) {
  return structuredClone({
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: SOURCE_KIND, form: 'setup' },
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
  const identity = createIdentityController({
    config,
    pluginVersion: PLUGIN_VERSION,
    logger,
    file: config.identityFile,
  })
  const identityState = identity.get()
  if (identityState.pending) {
    logger?.warn?.(
      `[atbrains] 身份待设置：缺少 ${identityState.missing.join('、') || '确认'}；首次对话会提示，或运行 /atbrains set machine=… os=… author=…`,
    )
  } else {
    logger?.debug?.(
      `[atbrains] 身份 machine=${identityState.machine} os=${identityState.os} author=${identityState.author}`,
    )
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

  /**
   * 服务端按令牌识别出的身份：machine/agent（客户端不能覆盖）。只用于展示与
   * 一致性告警（agent 与 platform 不符时说明库内会记成别的名字）；成功才缓存。
   */
  let serverIdentityCache = null
  async function serverIdentity() {
    if (serverIdentityCache) return serverIdentityCache
    if (!(await protocolOk())) return null
    try {
      const s = await client.status({ light: true })
      const machine = String(s?.machine || '')
      const agent = String(s?.agent || '')
      if (machine || agent) serverIdentityCache = { machine, agent }
      return serverIdentityCache
    } catch {
      return null
    }
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
    if ((lastCount.get(key) ?? -1) === t.total) return null
    const sessionId = String(session?.id ?? key)
    const prev = queues.get(key) || Promise.resolve()
    const run = prev.then(async () => {
      try {
        const res = await client.turn({
          session_id: sessionId,
          user: t.user,
          assistant: t.assistant,
          platform: config.platform,
          author: identity.get().author,
          messages: t.records,
          // 会话总条数：records 是尾部窗口（archiveMaxMessages），没有 total
          // 服务端会把「窗口长度 == 游标」误判成没有增量（0.3.2 的真实事故）。
          total: t.total,
        })
        lastCount.set(key, t.total)
        logger?.debug?.(
          `[atbrains] 已归档 turn=${payload?.turn} id=${res?.id} window=${t.records.length}/${t.total}`,
        )
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
    if (messages.some((m) => m?.source?.kind === SOURCE_KIND && m?.source?.form !== 'setup')) return decision
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

  /**
   * 首次安装/升级后的第一轮：身份缺值或未确认 → 注入一条 setup 提示。
   * 只注入一次（同一会话历史里已有 form='setup' 就跳过），保存后自然消失。
   */
  function setup(payload, decision) {
    if (!config.onboarding) return decision
    if (decision.kind !== 'enter') return decision
    if (payload?.step !== 1) return decision
    const messages = Array.isArray(decision.messages) ? decision.messages : []
    if (!messages.length) return decision
    if (!identity.get().pending) return decision
    if (messages.some((m) => m?.source?.kind === SOURCE_KIND && m?.source?.form === 'setup')) return decision
    if (!lastUserText(messages)) return decision
    return { kind: 'enter', messages: [...messages, setupMessage(identity.notice())] }
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
    identity,
    expectedAgent: String(config.platform || ''),
    serverIdentity,
    recall: (payload, decision) => recall(payload, decision),
    setup: (payload, decision) => setup(payload, decision),
    archive,
    observeEvent,
    _buffers: buffers,
    _lastCount: lastCount,
  }
}

// ---------------------------------------------------------------- 人机接口

/** `/atbrains` 命令体：直接返回 UI 文本，不进模型历史。 */
export async function identityCommandResult(runtime, rawInput) {
  const parsed = parseIdentityInput(rawInput)
  const identity = runtime.identity
  // 语法错误不探服务端；其余路径顺带展示令牌身份（失败静默，不影响命令结果）
  let server = null
  if (parsed.action !== 'invalid') {
    try {
      server = await runtime.serverIdentity?.()
    } catch {
      server = null
    }
  }
  const view = { server, expectedAgent: runtime.expectedAgent }
  if (parsed.action === 'help') {
    return { kind: 'success', text: `${IDENTITY_USAGE}\n\n${renderIdentity(identity.get(), view)}` }
  }
  if (parsed.action === 'show') {
    return { kind: 'success', text: renderIdentity(identity.get(), view) }
  }
  if (parsed.action === 'reset') {
    const res = identity.reset()
    return { kind: 'success', text: `${res.message}\n\n${renderIdentity(res.identity, view)}` }
  }
  if (parsed.action === 'invalid') {
    return { kind: 'error', text: `${parsed.error}\n${IDENTITY_USAGE}` }
  }
  try {
    const res = identity.set(parsed.patch)
    return { kind: 'success', text: `${res.message}\n\n${renderIdentity(res.identity, view)}` }
  } catch (error) {
    return { kind: 'error', text: `设置失败：${String(error?.message || error)}\n${IDENTITY_USAGE}` }
  }
}

/** 模型工具：首轮对话确认后保存，也允许用户以后用自然语言改。 */
export function identityToolResult(runtime, args = {}) {
  const identity = runtime.identity
  const action = String(args?.action || 'show').toLowerCase()
  if (action === 'show' || action === 'status') {
    return { ok: true, action: 'show', message: '当前本机身份。', identity: identity.get() }
  }
  if (action === 'reset') {
    const res = identity.reset()
    return { ok: true, action: 'reset', message: res.message, identity: res.identity }
  }
  if (action !== 'set') {
    return { ok: false, action, message: `未知 action：${action}（只支持 show / set / reset）`, identity: identity.get() }
  }
  const patch = {}
  for (const field of ['machine', 'os', 'author']) {
    if (args?.[field] !== undefined && args?.[field] !== null) patch[field] = args[field]
  }
  if (!Object.keys(patch).length) {
    return { ok: false, action, message: 'set 至少需要 machine / os / author 中的一个字段。', identity: identity.get() }
  }
  try {
    const res = identity.set(patch)
    return { ok: true, action: 'set', message: res.message, identity: res.identity }
  } catch (error) {
    return { ok: false, action, message: `设置失败：${String(error?.message || error)}`, identity: identity.get() }
  }
}

function registerIdentityCommand(ctx, runtime) {
  if (!ctx?.commands?.register) return undefined
  return ctx.commands.register({
    name: 'atbrains',
    description: '查看或重新设置本机 atbrains 身份（machine / os / author）',
    input: { hint: '[show|set machine=<机器名> os=<系统> author=<标识>|reset]' },
    handler: (invocation) => identityCommandResult(runtime, invocation?.rawInput),
  })
}

function registerIdentityTool(ctx, runtime) {
  if (!ctx?.tools?.register) return undefined
  return ctx.tools.register({
    name: 'atbrains_identity',
    description:
      'Show or update this machine\'s atbrains identity (machine, os, author). During first-run onboarding, ask the user to confirm the three values, then call action="set" with machine/os/author. action="reset" re-detects defaults and asks for confirmation again.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        action: {
          type: 'string',
          enum: ['show', 'set', 'reset'],
          description: 'show (default) to inspect, set to save fields, reset to re-detect defaults.',
        },
        machine: { type: 'string', description: 'Machine name, at most 48 characters.' },
        os: { type: 'string', description: 'Operating system label, at most 64 characters.' },
        author: {
          type: 'string',
          description: 'Author / human source tag, at most 64 characters (e.g. win:local, uid:1000, wx:<openid>).',
        },
      },
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          action: { type: 'string' },
          message: { type: 'string' },
          identity: {
            type: 'object',
            properties: {
              machine: { type: 'string' },
              os: { type: 'string' },
              author: { type: 'string' },
              confirmed: { type: 'boolean' },
              pending: { type: 'boolean' },
              missing: { type: 'array', items: { type: 'string' } },
              pluginVersion: { type: 'string' },
              firstInstall: { type: 'boolean' },
              versionChanged: { type: 'boolean' },
              file: { type: 'string' },
              origins: {
                type: 'object',
                properties: {
                  machine: { type: 'string' },
                  os: { type: 'string' },
                  author: { type: 'string' },
                },
              },
            },
          },
        },
      },
      render: (_args, value) => [
        { type: 'text', text: `${value?.message || ''}\n\n${renderIdentity(value?.identity || {})}` },
      ],
    },
    async execute(args) {
      return identityToolResult(runtime, args)
    },
  })
}

/**
 * 可选挂载 commands / tools：宿主没装这两个能力时插件照常召回/归档。
 * 返回清理函数（直接注册的测试路径需要它；Cordis 的 inject 子 fiber 自己回收）。
 */
export function registerIdentitySurfaces(ctx, runtime) {
  const disposers = []
  const install = (service, register) => {
    if (typeof ctx?.inject === 'function') {
      try {
        ctx.inject([service], (child) => {
          const off = register(child, runtime)
          if (typeof off === 'function') disposers.push(off)
        })
      } catch (error) {
        ctx?.logger?.warn?.(`[atbrains] 挂载 ${service} 失败，已跳过：${String(error?.message || error)}`)
      }
      return
    }
    if (ctx?.[service]) {
      try {
        const off = register(ctx, runtime)
        if (typeof off === 'function') disposers.push(off)
      } catch (error) {
        ctx?.logger?.warn?.(`[atbrains] 挂载 ${service} 失败，已跳过：${String(error?.message || error)}`)
      }
    }
  }
  install('commands', registerIdentityCommand)
  install('tools', registerIdentityTool)
  return () => {
    for (const off of disposers.splice(0)) {
      try {
        off()
      } catch {
        /* 卸载失败不影响其它监听器 */
      }
    }
  }
}

// ---------------------------------------------------------------- 插件入口

export function apply(ctx, rawConfig) {
  const runtime = createRuntime(ctx, rawConfig)
  if (!runtime.enabled) return
  const config = { ...DEFAULTS, ...(rawConfig || {}) }
  const identityState = runtime.identity.get()
  ctx.logger?.info?.(
    `[atbrains] 已启用 → ${runtime.client.baseUrl}（召回 + ${config.archiveEnabled ? '自动归档' : '不归档'}；` +
      `${identityState.pending ? '身份待设置' : `身份 ${identityState.machine}/${identityState.author || '-'}`}）`,
  )

  ctx.effect(() => {
    const offSurfaces = registerIdentitySurfaces(ctx, runtime)
    const offPre = ctx.on('agent/pre-step', async (payload, next) => {
      const decision = await next()
      return runtime.setup(payload, await runtime.recall(payload, decision))
    })
    const offEvent = ctx.on('session/event', (session, event) => runtime.observeEvent(session, event))
    const offStop = ctx.on('agent/turn-stopping', async (payload) => {
      await runtime.archive(payload)
    })
    return () => {
      offSurfaces?.()
      offPre?.()
      offEvent?.()
      offStop?.()
    }
  }, 'atbrains-memory: recall + archive')
}

export default { name, inject, Config, apply }
