/**
 * 协议版本护栏自测（离线）。
 *   1) health.version 主版本 = 2 → 正常工作
 *   2) health.version 主版本 = 3 → 停用（不注入、不归档），并打 error
 *   3) health 探活失败 → 不判死（交给真实请求降级）
 *
 * 跑：node test/protocol.mjs
 */
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRuntime } from '../lib/index.js'

// 身份状态不能写进真实 ~/.dsh
process.env.DSH_HOME = mkdtempSync(join(tmpdir(), 'dsh-atbrains-protocol-'))

const logs = []
const ctx = { logger: { info() {}, warn: (m) => logs.push(['warn', m]), error: (m) => logs.push(['error', m]), debug() {} } }

const userMsg = { id: 'u1', role: 'user', content: [{ type: 'text', text: '问点什么' }] }
const payload = { agent: { session: { id: 's-proto' } }, step: 1, turn: 1, messages: [userMsg] }
const enter = { kind: 'enter', messages: [userMsg] }

let calls = 0
function clientWith(health) {
  return () => ({
    baseUrl: 'mock',
    health,
    recall: async () => { calls += 1; return { text: '- [fact] x', items: [] } },
    turn: async () => { calls += 1; return { ok: true, id: 1 } },
  })
}

let failures = 0
function check(label, cond) {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}`)
  if (!cond) failures += 1
}

// 1) 同主版本
{
  const rt = createRuntime(ctx, { token: 't' }, { clientFactory: clientWith(async () => ({ ok: true, version: '2.0.0' })) })
  const out = await rt.recall(payload, enter)
  check('v2 正常注入', out.messages.length === 2)
}

// 2) 主版本不匹配 → 停用
{
  logs.length = 0
  calls = 0
  const rt = createRuntime(ctx, { token: 't' }, { clientFactory: clientWith(async () => ({ ok: true, version: '3.1.0' })) })
  const out = await rt.recall(payload, enter)
  check('v3 不注入', out.messages.length === 1)
  check('v3 打 error 日志', logs.some(([lvl, m]) => lvl === 'error' && m.includes('不匹配')))
  check('v3 不再发请求', calls === 0)
  const archived = await rt.archive({ agent: { session: { id: 's-proto' } }, turn: 1 })
  check('v3 归档也停用', archived === null)
}

// 3) 探活失败 → 不判死
{
  const rt = createRuntime(ctx, { token: 't' }, {
    clientFactory: clientWith(async () => { throw new Error('ECONNREFUSED') }),
  })
  const out = await rt.recall(payload, enter)
  check('探活失败仍可工作', out.messages.length === 2)
}

// 4) 探活只查一次（同一进程内）
{
  let probes = 0
  const rt = createRuntime(ctx, { token: 't' }, {
    clientFactory: clientWith(async () => { probes += 1; return { ok: true, version: '2.0.0' } }),
  })
  await rt.recall(payload, enter)
  await rt.recall(payload, enter)
  await rt.archive({ agent: { session: { id: 's-proto' } }, turn: 1 })
  check('health 只探一次', probes === 1)
}

console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项失败`)
process.exit(failures === 0 ? 0 : 1)
