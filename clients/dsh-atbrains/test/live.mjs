/**
 * 真机自测：打真实 atbrains 服务（默认本机 8787）。
 *
 * 令牌来源（任一）：
 *   ATBRAINS_MCP_TOKEN=<token> node test/live.mjs
 *   ATBRAINS_TOKEN_FILE=/path/to/token.txt node test/live.mjs   # 文件里只有一行令牌
 *
 * 干的事：health → status → recall → turn（写一轮带工具调用的对话）→ 打印 id。
 * 只在测试库会话 `zz-dsh-plugin-live` 上写，跑完自己打印该删的行。
 */
import { readFileSync } from 'node:fs'
import { AtbrainsClient, normalizeBaseUrl } from '../lib/client.js'
import { readSessionMessages, transcriptFromMessages } from '../lib/transcript.js'

const baseUrl = process.env.ATBRAINS_BASE_URL || 'http://127.0.0.1:8787'
let token = (process.env.ATBRAINS_MCP_TOKEN || '').trim()
if (!token && process.env.ATBRAINS_TOKEN_FILE) {
  token = readFileSync(process.env.ATBRAINS_TOKEN_FILE, 'utf8').split('\n')[0].trim()
}
if (!token) {
  console.error('缺令牌：设 ATBRAINS_MCP_TOKEN 或 ATBRAINS_TOKEN_FILE')
  process.exit(2)
}

const client = new AtbrainsClient({ baseUrl, token, timeoutMs: 4000, writeTimeoutMs: 8000 })
console.log('base =', normalizeBaseUrl(baseUrl))

const health = await client.health()
console.log('health =', JSON.stringify(health))

const status = await client.status()
console.log('status =', JSON.stringify({ machine: status.machine, agent: status.agent }))
console.log('stats  =', JSON.stringify(status.stats?.counts ?? {}).slice(0, 240))

const recalled = await client.recall('记忆库叫什么名字', { sessionId: 'zz-dsh-plugin-live' })
console.log('recall =', JSON.stringify({ count: recalled.count, textLen: String(recalled.text || '').length, first: recalled.items?.[0]?.kind }))

// 生成一份 dsh 形状的会话，跑真实的 surface 读取 + 映射
const events = [
  { seq: 0, msg: { role: 'user', content: [{ type: 'text', text: 'dsh 原生插件在线自测（可删）' }] } },
  { seq: 1, msg: { role: 'assistant', content: [{ type: 'tool-call', id: 'live_1', name: 'list_dir', arguments: '{"path":"."}' }] } },
  { seq: 2, msg: { role: 'tool', source: { kind: 'tool', callId: 'live_1' }, toolCallId: 'live_1', content: [{ type: 'text', text: 'a.txt' }] } },
  { seq: 3, msg: { role: 'assistant', content: [{ type: 'text', text: '只有一个 a.txt' }] } },
]
const session = {
  id: 'zz-dsh-plugin-live',
  surface: { nodes: events.map((e) => e.seq), deriveEventMessage: (ev) => ev.msg ?? null },
  eventAt: (seq) => events.find((e) => e.seq === seq) ?? null,
}
const msgs = readSessionMessages(session)
typeof msgs
const t = transcriptFromMessages(msgs, { maxChars: 16000 })
console.log('mapped =', JSON.stringify({ records: t.records.length, user: t.user.slice(0, 20), tool: t.records[2]?.tool_name }))

for (const round of [1, 2]) {
  const res = await client.turn({
    session_id: session.id,
    user: t.user,
    assistant: t.assistant,
    platform: process.env.ATBRAINS_PLATFORM || 'dsh',
    messages: t.records,
  })
  console.log(`turn#${round} =`, JSON.stringify(res))
}
console.log('\n清理：DELETE FROM brain_turn_messages WHERE turn_id IN (SELECT id FROM brain_turns WHERE session_id=\'zz-dsh-plugin-live\');')
console.log("       DELETE FROM brain_turns WHERE session_id='zz-dsh-plugin-live'; DELETE FROM brain_sessions WHERE session_id='zz-dsh-plugin-live';")
