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
    const { text, reasoning, calls } = splitBlocks(m?.content)
    if (role === 'assistant') {
      for (const c of calls) if (c.id) callNames.set(c.id, c.name)
      const rec = { role: 'assistant', content: redact(clip(text, maxChars)) }
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
      records.push({ role: 'user', content: redact(clip(text, maxChars)) })
      if (text) lastUser = text
    } else {
      const callId = String(m?.toolCallId ?? m?.tool_call_id ?? m?.source?.callId ?? '')
      records.push({
        role: 'tool',
        tool_name: callNames.get(callId) || '',
        tool_call_id: callId,
        content: redact(clip(text, maxChars)),
      })
    }
  }

  return {
    records: records.slice(-Math.max(1, maxMessages)),
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
    if (msg.role === 'user' || msg.role === 'assistant' || msg.role === 'tool') out.push(msg)
  }
  return out
}
