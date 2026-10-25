/**
 * dsh 会话 → atbrains 转录层 的消息映射。
 *
 * dsh 的 Message（@deepseek-ai/dsh-llm）是「role + ContentBlock[]」：
 *   user       : text / image / file
 *   assistant  : text / reasoning / tool-call
 *   tool       : text / image / file + toolCallId + isError
 * atbrains 的 brain_turn_messages 认的是「Hermes 形状」：
 *   {role, content, reasoning?, tool_calls?, tool_name?, tool_call_id?}
 * 这里做单向转换，丢掉 system/developer（atbrains 不存系统提示，per 项目约定）。
 */

const SECRET_PATTERNS = [
  /\bsk-[A-Za-z0-9_-]{16,}\b/g,
  /\bBearer\s+[A-Za-z0-9._~+/=-]{20,}/g,
  /\bAKIA[0-9A-Z]{12,}\b/g,
]

/** 抹掉明显的密钥痕迹：转录层是永久保存的，宁可少存一个字符也不要留明文。 */
export function redact(text) {
  let s = String(text ?? '')
  for (const re of SECRET_PATTERNS) s = s.replace(re, '[redacted]')
  return s
}

function clip(text, limit) {
  const s = String(text ?? '')
  return s.length <= limit ? s : `${s.slice(0, limit)}…`
}

/**
 * 取一条 dsh Message 的时间（epoch 秒/毫秒 或 ISO 串），取不到返回 undefined。
 *
 * 为什么要带上：服务端把转录投影成 brain_thoughts / brain_tool_calls 时，
 * 每行的时间取自消息里的 `timestamp`；dsh 早期版本不带这个字段，结果整段
 * 转录回填出来的 ts 全是 NULL（2026-10-25 在 turn 1150 上实测 177 行）。
 * dsh 的 event / message 上时间字段命名不统一，这里按常见名字逐个试。
 */
const TIME_FIELDS = ['timestamp', 'ts', 'at', 'time', 'createdAt', 'created_at']

export function msgTimestamp(m) {
  for (const src of [m, m?.source, m?.meta]) {
    if (!src || typeof src !== 'object') continue
    for (const k of TIME_FIELDS) {
      const v = src[k]
      if (typeof v === 'number' && Number.isFinite(v) && v > 0) return v
      if (typeof v === 'string' && v.trim()) return v.trim()
      if (v instanceof Date && !Number.isNaN(v.getTime())) return v.toISOString()
    }
  }
  return undefined
}

/** 把 ContentBlock[] 拆成文本 / 推理 / 工具调用 / 附件占位。 */
export function splitBlocks(blocks) {
  const texts = []
  const reasonings = []
  const calls = []
  const attachments = []
  for (const b of Array.isArray(blocks) ? blocks : []) {
    if (!b || typeof b !== 'object') continue
    switch (b.type) {
      case 'text':
        if (typeof b.text === 'string' && b.text) texts.push(b.text)
        break
      case 'reasoning':
        if (typeof b.text === 'string' && b.text) reasonings.push(b.text)
        break
      case 'tool-call':
        calls.push({
          id: String(b.id ?? ''),
          name: String(b.name ?? ''),
          arguments: typeof b.arguments === 'string' ? b.arguments : JSON.stringify(b.arguments ?? {}),
        })
        break
      case 'image':
        attachments.push('[图片]')
        break
      case 'file':
        attachments.push('[文件]')
        break
      default:
        break
    }
  }
  const text = [...texts, ...attachments].join('\n').trim()
  return { text, reasoning: reasonings.join('\n').trim(), calls }
}

/**
 * 判断一条 user 消息是不是「宿主/插件注入的假用户消息」。
 *
 * dsh 会把运行时上下文、可用 skill 清单、以及我们自己的召回结果都作为 **role='user'**
 * 的消息塞进模型可见面。它们不是用户说的话，落库会污染 user_content（曾出现过整轮
 * user_content 只有一段 [atbrains 长期记忆] 的事故），也会让转录里堆满系统噪声。
 *
 * 两道判据：source.kind 标记（我们自己注入的，可靠） + 已知注入前缀（宿主的，兜底）。
 */
const INJECTED_KIND = 'atbrains'
const SYNTHETIC_PREFIXES = ['[atbrains 记忆]', '[atbrains 长期记忆', '<system-reminder>', 'Current runtime context']

export function isSyntheticUser(m, text) {
  if (m?.role !== 'user') return false
  if (m?.source?.kind === INJECTED_KIND) return true
  const t = String(typeof text === 'string' ? text : splitBlocks(m?.content).text).trimStart()
  return SYNTHETIC_PREFIXES.some((p) => t.startsWith(p))
}

/**
 * dsh Message[] → (atbrains messages[], 本轮 user 文本, 本轮 assistant 文本)。
 *
 * `tool_name` 不在这条 tool 消息上（只有 callId），因此先从历史里的 tool-call 块
 * 建 callId→name 表再回填；找不到就留空（服务端能接受）。
 */
export function transcriptFromMessages(messages, { maxChars = 16000, maxMessages = 400 } = {}) {
  const callNames = new Map()
  const records = []
  let lastUser = ''
  let lastAssistant = ''

  for (const m of Array.isArray(messages) ? messages : []) {
    const role = String(m?.role || '')
    if (role !== 'user' && role !== 'assistant' && role !== 'tool') continue
    const ts = msgTimestamp(m)
    const { text, reasoning, calls } = splitBlocks(m?.content)
    if (role === 'user' && isSyntheticUser(m, text)) continue // 注入的假 user 消息不入库
    if (role === 'assistant') {
      for (const c of calls) if (c.id) callNames.set(c.id, c.name)
      const rec = { role: 'assistant', content: redact(clip(text, maxChars)) }
      if (ts !== undefined) rec.timestamp = ts
      if (reasoning) rec.reasoning = redact(clip(reasoning, maxChars))
      if (calls.length) {
        rec.tool_calls = calls.map((c) => ({
          id: c.id,
          type: 'function',
          function: { name: c.name, arguments: redact(clip(c.arguments, maxChars)) },
        }))
      }
      records.push(rec)
      if (text) lastAssistant = text
    } else if (role === 'user') {
      const rec = { role: 'user', content: redact(clip(text, maxChars)) }
      if (ts !== undefined) rec.timestamp = ts
      records.push(rec)
      if (text) lastUser = text
    } else {
      const callId = String(m?.toolCallId ?? m?.tool_call_id ?? m?.source?.callId ?? '')
      const rec = {
        role: 'tool',
        tool_name: callNames.get(callId) || '',
        tool_call_id: callId,
        content: redact(clip(text, maxChars)),
      }
      if (ts !== undefined) rec.timestamp = ts
      records.push(rec)
    }
  }

  return {
    records: records.slice(-Math.max(1, maxMessages)),
    total: records.length, // 截断前的总条数：服务端游标是按绝对条数记的
    user: redact(clip(lastUser, maxChars)),
    assistant: redact(clip(lastAssistant, maxChars)),
  }
}

/**
 * 从 dsh Session 读出当前完整对话（模型可见面）。
 *
 * 用 surface.nodes + eventAt + deriveEventMessage：这是「模型看到的那串消息」，
 * 压缩/替换后的形态也已被折叠，服务端按条数游标只落增量。老版本 dsh 若没有
 * 这些 API，返回空数组，由调用方的 session/event 缓冲区兜底。
 */
export function readSessionMessages(session) {
  const out = []
  const surface = session?.surface
  const nodes = surface?.nodes
  if (!nodes || typeof nodes[Symbol.iterator] !== 'function') return out
  if (typeof session?.eventAt !== 'function' || typeof surface?.deriveEventMessage !== 'function') return out
  for (const seq of nodes) {
    let event = null
    try {
      event = session.eventAt(seq)
    } catch {
      event = null
    }
    if (!event) continue
    let msg = null
    try {
      msg = surface.deriveEventMessage(event)
    } catch {
      msg = null
    }
    if (!msg) continue
    if (msg.role === 'user' || msg.role === 'assistant' || msg.role === 'tool') {
      // 消息本身没带时间就用事件上的（dsh 的 event 通常有 at/ts），别让它落到库里变 NULL
      if (msgTimestamp(msg) === undefined) {
        const ts = msgTimestamp(event)
        if (ts !== undefined) msg = { ...msg, timestamp: ts }
      }
      out.push(msg)
    }
  }
  return out
}
