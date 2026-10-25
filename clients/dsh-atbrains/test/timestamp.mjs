/**
 * 时间戳透传自测（不联网）。
 *
 * 服务端把转录投影成 brain_thoughts / brain_tool_calls 时，每行的时间取自消息
 * 上的 `timestamp`；dsh 0.3.3 及更早不带，回填出来的 ts 全是 NULL。
 * 这里验证 0.3.4 的三种来源都能透传，且不影响原有字段。
 *
 * 跑：node test/timestamp.mjs
 */
import { transcriptFromMessages, readSessionMessages, msgTimestamp } from '../lib/transcript.js'

let failures = 0
function check(name, ok) {
  console.log(`${ok ? '✓' : '✗'} ${name}`)
  if (!ok) failures += 1
}

const msgs = [
  { role: 'user', timestamp: 1761340800, content: [{ type: 'text', text: '带 epoch 秒' }] },
  { role: 'assistant', ts: '2026-10-24T18:29:43Z', content: [{ type: 'reasoning', text: '想一下' }, { type: 'text', text: '好' }] },
  { role: 'assistant', content: [{ type: 'tool-call', id: 'c1', name: 'list_dir', arguments: '{}' }], source: { at: 1761340825 } },
  { role: 'tool', toolCallId: 'c1', source: { kind: 'tool', callId: 'c1', at: 1761340826 }, content: [{ type: 'text', text: 'a.txt' }] },
  { role: 'assistant', content: [{ type: 'text', text: '没有时间的消息' }] },
]

const t = transcriptFromMessages(msgs)
check('user 的 epoch 秒透传', t.records[0].timestamp === 1761340800)
check('assistant 的 ISO 串透传', t.records[1].timestamp === '2026-10-24T18:29:43Z')
check('tool_call 消息从 source.at 取时间', t.records[2].timestamp === 1761340825)
check('tool 结果从 source.at 取时间', t.records[3].timestamp === 1761340826)
check('没有时间的消息不伪造字段', !('timestamp' in t.records[4]))
check('原有字段没被影响', t.records[2].tool_calls?.[0]?.function?.name === 'list_dir' && t.records[1].reasoning === '想一下')

check('msgTimestamp 认 Date 实例', msgTimestamp({ at: new Date('2026-10-24T18:30:00Z') }) === '2026-10-24T18:30:00.000Z')
check('msgTimestamp 无字段返回 undefined', msgTimestamp({ role: 'user' }) === undefined)

// readSessionMessages：消息没带时间就用事件上的
const events = [
  { seq: 0, at: 1761340900, msg: { role: 'user', content: [{ type: 'text', text: '问' }] } },
  { seq: 1, at: 1761340901, msg: { role: 'assistant', content: [{ type: 'text', text: '答' }] } },
]
const session = {
  surface: {
    nodes: events.map((e) => e.seq),
    deriveEventMessage: (e) => e.msg,
  },
  eventAt: (seq) => events.find((e) => e.seq === seq) ?? null,
}
const out = readSessionMessages(session)
check('事件时间补到消息上', out[0].timestamp === 1761340900 && out[1].timestamp === 1761340901)

const kept = readSessionMessages({
  surface: {
    nodes: [0],
    deriveEventMessage: () => ({ role: 'user', timestamp: 111, content: [{ type: 'text', text: '自带时间' }] }),
  },
  eventAt: () => ({ at: 222 }),
})
check('消息自带时间优先于事件', kept[0].timestamp === 111)

console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项失败`)
process.exit(failures === 0 ? 0 : 1)
