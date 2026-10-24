/**
 * 装配自测：走 apply() 真实注册路径（不联网，地址故意指到不可达端口）。
 * 验证插件在 dsh 里被加载时会挂上三个监听，且服务不可达时静默降级、不抛异常。
 *
 * 跑：node test/wiring.mjs
 */
import { apply } from '../lib/index.js'

const handlers = new Map()
const logs = []
const ctx = {
  logger: {
    info: (m) => logs.push(['info', m]),
    warn: (m) => logs.push(['warn', m]),
    error: (m) => logs.push(['error', m]),
    debug: () => {},
  },
  on: (event, fn) => {
    handlers.set(event, fn)
    return () => handlers.delete(event)
  },
  effect: (fn) => {
    fn()
    return () => {}
  },
}

let failures = 0
function check(label, cond) {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}`)
  if (!cond) failures += 1
}

apply(ctx, { token: 'wiring-test', baseUrl: 'http://127.0.0.1:1', timeoutMs: 300, writeTimeoutMs: 300 })

check('启用日志已打印', logs.some(([lvl, m]) => lvl === 'info' && m.includes('已启用')))
check('注册了 agent/pre-step', typeof handlers.get('agent/pre-step') === 'function')
check('注册了 agent/turn-stopping', typeof handlers.get('agent/turn-stopping') === 'function')
check('注册了 session/event', typeof handlers.get('session/event') === 'function')

// 服务不可达：召回应原样放行并 warn
const userMsg = { id: 'u1', role: 'user', content: [{ type: 'text', text: '任意问题' }] }
const decision = await handlers.get('agent/pre-step')(
  { agent: { session: { id: 's1' } }, step: 1, turn: 1, messages: [userMsg] },
  async () => ({ kind: 'enter', messages: [userMsg] }),
)
check('不可达时不注入、不抛', decision.kind === 'enter' && decision.messages.length === 1)
check('不可达时打 warn', logs.some(([lvl, m]) => lvl === 'warn' && m.includes('召回失败')))

// 归档：同样不许抛
await handlers.get('agent/turn-stopping')({ agent: { session: { id: 's1' } }, turn: 1 })
check('归档失败不影响回合', true)

console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项失败`)
process.exit(failures === 0 ? 0 : 1)
