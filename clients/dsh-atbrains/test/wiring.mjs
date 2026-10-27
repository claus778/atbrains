/**
 * 装配自测：走 apply() 真实注册路径（不联网，地址故意指到不可达端口）。
 * 验证：
 *   · 插件在 dsh 里挂上三个监听；
 *   · 首次安装身份未确认 → 第一轮注入 setup 提示；
 *   · /atbrains 命令与 atbrains_identity 工具都注册并且能保存值；
 *   · 保存后不再注入；服务不可达时静默降级、不抛异常。
 *
 * 跑：node test/wiring.mjs
 */
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { apply, createRuntime, identityCommandResult } from '../lib/index.js'

const home = mkdtempSync(join(tmpdir(), 'dsh-atbrains-wiring-'))
process.env.DSH_HOME = home

const handlers = new Map()
const commands = new Map()
const tools = new Map()
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
  commands: {
    register: (definition) => {
      commands.set(definition.name, definition)
      return () => commands.delete(definition.name)
    },
  },
  tools: {
    register: (definition) => {
      tools.set(definition.name, definition)
      return () => tools.delete(definition.name)
    },
  },
}

let failures = 0
function check(label, cond) {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}`)
  if (!cond) failures += 1
}

apply(ctx, {
  token: 'wiring-test',
  baseUrl: 'http://127.0.0.1:1',
  timeoutMs: 300,
  writeTimeoutMs: 300,
  identityFile: join(home, 'identity.json'),
})

check('启用日志已打印', logs.some(([lvl, m]) => lvl === 'info' && m.includes('已启用')))
check('身份未完成时打 warning', logs.some(([lvl, m]) => lvl === 'warn' && m.includes('身份待设置')))
check('注册了 agent/pre-step', typeof handlers.get('agent/pre-step') === 'function')
check('注册了 agent/turn-stopping', typeof handlers.get('agent/turn-stopping') === 'function')
check('注册了 session/event', typeof handlers.get('session/event') === 'function')
check('注册了 /atbrains 命令', commands.has('atbrains'))
check('注册了 atbrains_identity 工具', tools.has('atbrains_identity'))

const userMsg = { id: 'u1', role: 'user', content: [{ type: 'text', text: '任意问题' }] }
const preStep = (sessionId, messages = [userMsg]) =>
  handlers.get('agent/pre-step')(
    { agent: { session: { id: sessionId } }, step: 1, turn: 1, messages },
    async () => ({ kind: 'enter', messages }),
  )

// 首次对话：服务不可达也要注入 setup（召回失败只 warn）
{
  const decision = await preStep('s1')
  check('不可达时仍返回 enter', decision.kind === 'enter')
  const setup = decision.messages.find((m) => m?.source?.form === 'setup')
  check('首次对话注入 setup 提示', Boolean(setup))
  check('setup 提示带初始化说明', String(setup?.content?.[0]?.text || '').includes('[atbrains 初始化]'))
  check('不可达时召回打 warn', logs.some(([lvl, m]) => lvl === 'warn' && m.includes('召回失败')))
}

// /atbrains set → 保存并确认
{
  const result = await commands.get('atbrains').handler({
    rawInput: 'set machine=r9000p os=Windows 11 author=win:local',
  })
  check('命令 set 成功', result.kind === 'success')
  check('命令回显 machine', result.text.includes('r9000p'))
  check('命令回显 author', result.text.includes('win:local'))
}

// 已确认后：新会话不再注入 setup
{
  const decision = await preStep('s2')
  check('确认后不再注入 setup', !decision.messages.some((m) => m?.source?.form === 'setup'))
}

// 工具：show / set / reset
{
  const tool = tools.get('atbrains_identity')
  const shown = await tool.execute({ action: 'show' })
  check('工具 show 返回身份', shown.ok === true && shown.identity.machine === 'r9000p')
  const set = await tool.execute({ action: 'set', author: 'tool-author' })
  check('工具 set 保存 author', set.ok === true && set.identity.author === 'tool-author')
  const rendered = tool.output.render({}, set)
  check('工具输出可渲染', rendered[0].type === 'text' && rendered[0].text.includes('tool-author'))
  const reset = await tool.execute({ action: 'reset' })
  check('工具 reset 重新待设置', reset.ok === true && reset.identity.pending === true)
  const bad = await tool.execute({ action: 'bogus' })
  check('工具未知 action 返回 ok:false', bad.ok === false)
}

// 命令 show / reset / invalid
{
  const command = commands.get('atbrains')
  const shown = await command.handler({ rawInput: '' })
  check('命令 show 输出状态', shown.kind === 'success' && shown.text.includes('状态'))
  const reset = await command.handler({ rawInput: 'reset' })
  check('命令 reset 成功', reset.kind === 'success' && reset.text.includes('待设置'))
  const invalid = await command.handler({ rawInput: 'set nope=1' })
  check('命令未知字段返回 error', invalid.kind === 'error')
}

// 可选注入路径：真实 dsh 里 ctx.inject(['commands'|'tools'], cb) 才拿得到服务
{
  const injected = new Map()
  const child = {
    commands: { register: (definition) => (injected.set(`cmd:${definition.name}`, definition), () => {}) },
    tools: { register: (definition) => (injected.set(`tool:${definition.name}`, definition), () => {}) },
  }
  const deps = []
  const ctx2 = {
    logger: { info() {}, warn() {}, error() {}, debug() {} },
    on: () => () => {},
    effect: (fn) => {
      fn()
      return () => {}
    },
    inject: (names, callback) => {
      deps.push(...names)
      callback(child)
      return { dispose() {} }
    },
  }
  apply(ctx2, {
    token: 'wiring-test-2',
    baseUrl: 'http://127.0.0.1:1',
    identityFile: join(home, 'identity-inject.json'),
  })
  check('可选用 ctx.inject 请求 commands/tools', deps.includes('commands') && deps.includes('tools'))
  check('注入路径注册 /atbrains', injected.has('cmd:atbrains'))
  check('注入路径注册 atbrains_identity', injected.has('tool:atbrains_identity'))
}

// 服务端令牌身份：展示 + agent 不一致时告警（/v1 的 agent 只能由令牌决定）
{
  const runtimeWith = (agent) => {
    const ctx2 = { logger: { info() {}, warn() {}, error() {}, debug() {} } }
    return createRuntime(
      ctx2,
      { token: 't', platform: 'dsh', identityFile: join(home, `identity-server-${agent}.json`) },
      {
        clientFactory: () => ({
          baseUrl: 'mock',
          health: async () => ({ ok: true, version: '2.0.0' }),
          status: async () => ({ ok: true, machine: 'r9000p-Windows', agent }),
          recall: async () => ({}),
          turn: async () => ({}),
        }),
      },
    )
  }
  const mismatch = await identityCommandResult(runtimeWith('hermes'), 'show')
  check('展示服务端 machine/agent', mismatch.text.includes('machine=r9000p-Windows') && mismatch.text.includes('agent=hermes'))
  check('agent 与 platform 不符时告警', mismatch.text.includes('token-add'))
  const match = await identityCommandResult(runtimeWith('dsh'), 'show')
  check('agent 一致时不告警', match.text.includes('agent=dsh') && !match.text.includes('token-add'))
}

// 归档：服务不可达也不许抛
await handlers.get('agent/turn-stopping')({ agent: { session: { id: 's1' } }, turn: 1 })
check('归档失败不影响回合', true)

console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项失败`)
process.exit(failures === 0 ? 0 : 1)
