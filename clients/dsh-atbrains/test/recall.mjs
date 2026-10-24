/**
 * 召回链路自测（不联网）。验证：
 *   1) step=1 且 decision 是 enter 时，注入一条 source.kind='atbrains' 的消息；
 *   2) 本轮已经有注入过就不再注入（防工具循环重复）；
 *   3) 召回失败/无命中时原样返回 decision，不抛。
 *
 * 跑：node test/prestep.mjs
 */
import { createRuntime } from '../lib/index.js'

const logs = []
const ctx = { logger: { info: (m) => logs.push(['info', m]), warn: (m) => logs.push(['warn', m]), error: (m) => logs.push(['error', m]), debug: () => {} } }

function fakeClient(res) {
  return () => ({
    baseUrl: 'mock',
    health: async () => ({ ok: true, version: '2.0.0' }),
    recall: async () => res,
    turn: async () => ({ ok: true, id: 1 }),
  })
}

const config = {
  token: 'test-token',
  minScore: 0,
}

const userMsg = { id: 'u1', role: 'user', content: [{ type: 'text', text: '我们的记忆库叫什么名字' }] }
const payload = { agent: { session: { id: 'sess-mock' } }, step: 1, turn: 1, messages: [userMsg] }
const enter = { kind: 'enter', messages: [userMsg] }

let failures = 0
function check(label, cond) {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}`)
  if (!cond) failures += 1
}

// case 1：命中 → 注入
{
  const rt = createRuntime(ctx, config, {
    clientFactory: fakeClient({ text: '- [fact] 记忆库叫 agent_memory', items: [{ kind: 'fact', vscore: 0.8 }] }),
  })
  const out = await rt.recall(payload, enter)
  check('step=1 注入一条消息', out.kind === 'enter' && out.messages.length === 2)
  const injected = out.messages.at(-1)
  check("注入消息 source.kind='atbrains'", injected?.source?.kind === 'atbrains')
  check("注入消息 form='recall'", injected?.source?.form === 'recall')
  check('注入文本含命中内容', String(injected?.content?.[0]?.text).includes('agent_memory'))
  check('原消息未被改写', out.messages[0] === userMsg)
}

// case 2：已注入过 → 跳过
{
  const rt = createRuntime(ctx, config, { clientFactory: fakeClient({ text: 'x', items: [] }) })
  const once = await rt.recall(payload, enter)
  const twice = await rt.recall(payload, once)
  check('已注入过不重复注入', twice.messages.length === 2)
}

// case 3：step=2 跳过
{
  const rt = createRuntime(ctx, config, { clientFactory: fakeClient({ text: 'x', items: [] }) })
  const out = await rt.recall({ ...payload, step: 2 }, enter)
  check('step=2 不注入', out.messages.length === 1)
}

// case 4：召回抛错 → 原样返回
{
  const rt = createRuntime(ctx, config, {
    clientFactory: () => ({
      baseUrl: 'mock',
      health: async () => ({ ok: true, version: '2.0.0' }),
      recall: async () => { throw new Error('HTTP 503') },
      turn: async () => ({}),
    }),
  })
  const out = await rt.recall(payload, enter)
  check('召回失败不阻塞', out.kind === 'enter' && out.messages.length === 1)
  check('失败有 warn 日志', logs.some(([lvl, m]) => lvl === 'warn' && m.includes('召回失败')))
}

// case 5：无命中 → 不注入
{
  const rt = createRuntime(ctx, config, { clientFactory: fakeClient({ text: '', items: [] }) })
  const out = await rt.recall(payload, enter)
  check('无命中不注入空消息', out.messages.length === 1)
}

// case 6：pre-step 返回 reject 时不动
{
  const rt = createRuntime(ctx, config, { clientFactory: fakeClient({ text: 'x', items: [] }) })
  const out = await rt.recall(payload, { kind: 'reject' })
  check('reject 原样透传', out.kind === 'reject')
}

// case 7：dsh 把运行时上下文 / skill 清单也当 user 消息 → 召回查询必须仍是真实提问
{
  const seen = []
  const rt = createRuntime(ctx, config, {
    clientFactory: () => ({
      baseUrl: 'mock',
      health: async () => ({ ok: true, version: '2.0.0' }),
      recall: async (q) => {
        seen.push(q)
        return { text: '- [fact] 命中', items: [{ kind: 'fact', vscore: 1 }] }
      },
      turn: async () => ({}),
    }),
  })
  const noisy = [
    userMsg,
    { role: 'user', content: [{ type: 'text', text: 'Current runtime context. This snapshot supersedes earlier ones.' }] },
    { role: 'user', content: [{ type: 'text', text: '<system-reminder>\nA skill is a reusable set…' }] },
  ]
  const out = await rt.recall({ ...payload, messages: noisy }, { kind: 'enter', messages: noisy })
  check('召回查询跳过假 user 消息', seen[0] === '我们的记忆库叫什么名字')
  check('注入追加在末尾', out.messages.at(-1)?.source?.kind === 'atbrains' && out.messages.length === 4)
}

console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项失败`)
process.exit(failures === 0 ? 0 : 1)
