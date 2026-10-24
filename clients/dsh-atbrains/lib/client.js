/**
 * atbrains /v1 HTTP 客户端 —— 纯 fetch，无 Node 原生依赖、无子进程、无 Python。
 *
 * 端点（见服务端 plugin/service.py 的 _ROUTES）：
 *   POST /v1/memory/recall    {query, session_id}      → {text, count, items[]}
 *   POST /v1/memory/turn      {session_id, user, assistant, platform, author, messages[]}
 *   POST /v1/memory/remember  {content, kind, tags, importance}
 *   POST /v1/memory/forget    {id}
 *   POST /v1/memory/status                              → {machine, agent, stats}
 *   GET  /v1/health
 *
 * 令牌按 Bearer 传；服务端用它反查身份（`<主机>|<agent>`），客户端不能伪造 machine。
 */

export class AtbrainsHttpError extends Error {
  constructor(message, status) {
    super(message)
    this.name = 'AtbrainsHttpError'
    this.status = status
  }
}

/** 允许用户把 baseUrl 写成 `http://host:8787` 或旧的 `http://host:8787/mcp`。 */
export function normalizeBaseUrl(raw) {
  let s = String(raw || '').trim()
  if (!s) s = 'http://127.0.0.1:8787'
  if (!/^https?:\/\//i.test(s)) s = `http://${s}`
  s = s.replace(/\/+$/, '')
  s = s.replace(/\/mcp$/i, '')
  s = s.replace(/\/v1$/i, '')
  return s
}

export class AtbrainsClient {
  /**
   * @param {object} opts
   * @param {string} opts.baseUrl 服务地址（无需带 /v1）
   * @param {string} opts.token   Bearer 令牌
   * @param {number} [opts.timeoutMs] 召回等轻请求超时
   * @param {number} [opts.writeTimeoutMs] 归档等写请求超时
   */
  constructor({ baseUrl, token, timeoutMs = 1200, writeTimeoutMs = 4000 }) {
    this.baseUrl = normalizeBaseUrl(baseUrl)
    this.token = String(token || '')
    this.timeoutMs = Math.max(200, Number(timeoutMs) || 1200)
    this.writeTimeoutMs = Math.max(500, Number(writeTimeoutMs) || 4000)
  }

  async #request(method, path, body, timeoutMs) {
    const url = `${this.baseUrl}${path}`
    const init = {
      method,
      headers: {
        accept: 'application/json',
        authorization: `Bearer ${this.token}`,
      },
      signal: AbortSignal.timeout(timeoutMs),
    }
    if (body !== undefined) {
      init.headers['content-type'] = 'application/json'
      init.body = JSON.stringify(body)
    }
    const res = await fetch(url, init)
    const raw = await res.text()
    let data = null
    if (raw.trim()) {
      try {
        data = JSON.parse(raw)
      } catch {
        data = { raw: raw.slice(0, 500) }
      }
    }
    if (!res.ok) {
      const reason = data?.error || data?.message || raw.slice(0, 200)
      throw new AtbrainsHttpError(`HTTP ${res.status}: ${reason}`, res.status)
    }
    return data ?? {}
  }

  /** 探活（不需要令牌，用于安装自检）。 */
  health() {
    return this.#request('GET', '/v1/health', undefined, this.timeoutMs)
  }

  /** 语义+关键词混合召回；返回服务端已排好序的注入文本与结构化命中。 */
  recall(query, { sessionId = '', limit } = {}) {
    const body = { query: String(query || '').slice(0, 2000) }
    if (sessionId) body.session_id = String(sessionId)
    if (limit) body.limit = Number(limit)
    return this.#request('POST', '/v1/memory/recall', body, this.timeoutMs)
  }

  /** 归档一轮对话；messages 是「到目前为至的完整会话」，服务端按游标只落增量。 */
  turn(payload) {
    return this.#request('POST', '/v1/memory/turn', payload, this.writeTimeoutMs)
  }

  remember(content, { kind = 'note', tags = '', importance = 5 } = {}) {
    return this.#request('POST', '/v1/memory/remember',
      { content: String(content), kind, tags, importance }, this.writeTimeoutMs)
  }

  forget(id) {
    return this.#request('POST', '/v1/memory/forget', { id: Number(id) }, this.writeTimeoutMs)
  }

  status() {
    return this.#request('POST', '/v1/memory/status', {}, this.timeoutMs)
  }
}
