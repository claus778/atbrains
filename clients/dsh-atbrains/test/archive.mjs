/**
 * 归档链路自测（不联网）。用一个假 Session（surface.nodes + eventAt +
 * deriveEventMessage）喂 dsh 形状的 Message，验证转录映射与提交载荷。
 *
 * 跑：node test/archive.mjs
 */
import { createRuntime } from '../lib/index.js'
import { transcriptFromMessages } from '../lib/transcript.js'

const events = [
  { seq: 0, msg: { role: 'system', content: [{ type: 'text', text: '系统提示不入库' }] } },
  { seq: 1, msg: { role: 'user', content: [{ type: 'text', text: '看看当前目录有什么' }] } },
  {
    seq: 2,
    msg: {
      role: 'assistant',
      content: [
        { type: 'reasoning', text: '先列目录' },
        { type: 'tool-call', id: 'call_1', name: 'list_dir', arguments: '{"path":"."}' },
      ],
    },
  },
  { seq: 3, msg: { role: 'tool', source: { kind: 'tool', callId: 'call_1' }, toolCallId: 'call_1', content: [{ type: 'text', text: 'a.txt\nb.txt' }] } },
  { seq: 4, msg: { role: 'assistant', content: [{ type: 'text', text: '目录里有两个文件：a.txt、b.txt' }] } },
]

const session = {
  id: 'zz-dsh-plugin-test',
  surface: {
    nodes: events.map((e) => e.seq),
    deriveEventMessage: (ev) => ev.msg ?? null,
  },
  eventAt: (seq) => events.find((e) => e.seq === seq) ?? null,
}

const sent = []
const ctx = { logger: { info() {}, warn: (m) => console.log('warn:', m), error() {}, debug() {} } }
const config = { token: 'test-token', platform: 'dsh' }
const rt = createRuntime(ctx, config, {
  clientFactory: () => ({
    baseUrl: 'mock',
    health: async () => ({ ok: true, version: '2.0.0' }),
    recall: async () => ({}),
    turn: async (body) => {
      sent.push(body)
      return { ok: true, id: sent.length }
    },
  }),
})

let failures = 0
function check(label, cond) {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}`)
  if (!cond) failures += 1
}

// 1) 纯函数：映射
{
  const t = transcriptFromMessages(events.map((e) => e.msg))
  check('system 被丢弃', t.records.length === 4)
  check('user / assistant / tool 角色齐', t.records.map((r) => r.role).join(',') === 'user,assistant,tool,assistant')
  check('assistant 带 tool_calls', t.records[1].tool_calls?.[0]?.function?.name === 'list_dir')
  check('assistant 带 reasoning', t.records[1].reasoning === '先列目录')
  check('tool 消息回填 tool_name', t.records[2].tool_name === 'list_dir')
  check('tool 消息带 call id', t.records[2].tool_call_id === 'call_1')
  check('user 文本正确', t.user === '看看当前目录有什么')
  check('assistant 文本取最后一条', t.assistant.includes('a.txt'))
}

// 2) 端到端（假 client）：提交载荷形状
{
  const res = await rt.archive({ agent: { session }, turn: 7 })
  check('归档返回服务端 id', res?.id === 1)
  const body = sent[0]
  check('session_id 取自 session.id', body.session_id === 'zz-dsh-plugin-test')
  check('platform=dsh', body.platform === 'dsh')
  check('messages 是完整链', Array.isArray(body.messages) && body.messages.length === 4)
  check('author 留空由库侧回填', body.author === '')
}

// 3) 同一会话重复触发且消息没变 → 不再提交
{
  const before = sent.length
  await rt.archive({ agent: { session }, turn: 7 })
  check('条数未变则不重复提交', sent.length === before)
}

// 4) 新增消息后继续提交
{
  const more = [...events, { seq: 5, msg: { role: 'user', content: [{ type: 'text', text: '继续' }] } }]
  session.surface.nodes = more.map((e) => e.seq)
  session.eventAt = (seq) => more.find((e) => e.seq === seq) ?? null
  await rt.archive({ agent: { session }, turn: 8 })
  check('新消息触发再次提交', sent.length === 2 && sent[1].messages.length === 5)
}

// 5) 密钥脱敏
{
  const t = transcriptFromMessages([
    { role: 'user', content: [{ type: 'text', text: '我的 key 是 sk-abcdefghijklmnopqrstuvwx 别存' }] },
  ])
  check('sk- 密钥被抹掉', !t.records[0].content.includes('sk-abcdefghijklmnopqrstuvwx'))
}

console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项失败`)
process.exit(failures === 0 ? 0 : 1)
