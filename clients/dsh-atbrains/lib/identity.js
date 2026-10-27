/**
 * 本机身份（machine / os / author）—— 安装向导、持久化与解析。
 *
 * 为什么要单独一层：
 *   · /v1 的 machine 由服务端按令牌反查，客户端不能伪造；但插件仍需要一份
 *     本机可展示、可确认的身份（尤其是 author，空值会被库侧回填成 unknown）。
 *   · 插件升级会覆盖安装目录，所以状态写在 `$DSH_HOME/atbrains/identity.json`
 *     （默认 `~/.dsh/atbrains/identity.json`），不写插件目录、不改 YAML 配置。
 *   · 第一次安装或升级后发现缺值 → setupPending；首次对话由 pre-step 注入一条
 *     待办提示，模型用 `atbrains_identity` 工具或用户用 `/atbrains` 命令保存。
 *
 * 配置优先级：identity.json（用户确认/保存） > 插件 config（machine/os/author）
 * > 自动检测（hostname / 系统版本；author 不臆造，保持空并提示）。
 */

import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import { homedir, hostname as osHostname, release as osRelease, type as osType } from 'node:os'
import { dirname, join, relative, resolve, sep } from 'node:path'

/** identity.json 的结构版本；字段不兼容时递增。 */
export const IDENTITY_SCHEMA = 1

/** 固定字段顺序，展示与 diff 都依赖它。 */
export const IDENTITY_FIELDS = Object.freeze(['machine', 'os', 'author'])

/** 与库列宽对齐：brain_turns.machine VARCHAR(48) / author VARCHAR(64)。 */
export const FIELD_LIMITS = Object.freeze({ machine: 48, os: 64, author: 64 })

export const FIELD_LABELS = Object.freeze({
  machine: '机器名',
  os: '操作系统',
  author: '作者',
})

const ORIGIN_LABELS = Object.freeze({
  saved: '已保存',
  config: '配置文件',
  detected: '自动检测',
  empty: '空',
})

/** 中文/英文键名都接受，避免用户为了一个字段去查文档。 */
const KEY_ALIASES = Object.freeze({
  machine: 'machine',
  host: 'machine',
  hostname: 'machine',
  name: 'machine',
  机器: 'machine',
  机器名: 'machine',
  主机: 'machine',
  主机名: 'machine',
  os: 'os',
  system: 'os',
  platform: 'os',
  系统: 'os',
  操作系统: 'os',
  author: 'author',
  user: 'author',
  human: 'author',
  作者: 'author',
  作者信息: 'author',
  人类: 'author',
})

function nowIso(now) {
  try {
    return now().toISOString()
  } catch {
    return new Date().toISOString()
  }
}

/** 去掉控制字符与多余空白；不截断（截断交给检测路径，用户输入必须报错）。 */
function collapse(raw) {
  return String(raw ?? '')
    .replace(/[\u0000-\u001f\u007f]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

/** 用户输入的值：校验字段名与长度，超长直接报错，绝不静默截断。 */
export function cleanValue(field, raw) {
  const max = FIELD_LIMITS[field]
  if (!max) throw new Error(`未知身份字段：${field}`)
  const value = collapse(raw)
  if (value.length > max) {
    throw new Error(`${FIELD_LABELS[field]}最长 ${max} 个字符（当前 ${value.length}）`)
  }
  return value
}

/** 自动检测值允许截断：宁可给一个可用默认值，也不要让插件启动失败。 */
function clipValue(field, raw) {
  const max = FIELD_LIMITS[field]
  return collapse(raw).slice(0, max)
}

/** 展开 `~`、`~/…`、`~\…`；与 @deepseek-ai/dsh-home-paths 同口径。 */
export function expandHomePath(path) {
  const raw = String(path ?? '')
  if (raw === '~') return homedir()
  if (raw.startsWith('~/') || raw.startsWith('~\\')) return join(homedir(), raw.slice(2))
  return raw
}

/** `$DSH_HOME` 优先，其次 `~/.dsh`（与 @deepseek-ai/dsh-home-paths 同口径）。 */
export function dshHome(env = process.env) {
  const configured = String(env?.DSH_HOME || '').trim()
  return resolve(expandHomePath(configured || join(homedir(), '.dsh')))
}

export function defaultIdentityFile(env = process.env) {
  return join(dshHome(env), 'atbrains', 'identity.json')
}

export function detectMachine() {
  const raw = osHostname() || 'unknown'
  return clipValue('machine', raw.replace(/\s+/g, '-'))
}

/**
 * Windows 只报 10.0.<build>，用 build 号判断 10/11，比 `os.type()` 可读。
 * 其他平台保留 `Linux <release>` / `macOS <release>`。
 */
export function detectOs() {
  if (process.platform === 'win32') {
    const rel = osRelease()
    const build = Number(rel.split('.')[2] || 0)
    const family = build >= 22000 ? 'Windows 11' : 'Windows 10'
    return clipValue('os', `${family} (${rel})`)
  }
  if (process.platform === 'darwin') return clipValue('os', `macOS ${osRelease()}`)
  return clipValue('os', `${osType()} ${osRelease()}`)
}

/**
 * author 是有语义的人类/来源标识，不能拿账号名硬猜（可能把管理员名写进共享库）。
 * 只接受显式环境变量；命令行/工具再让用户确认。
 */
export function detectAuthor(env = process.env) {
  return clipValue('author', env?.ATBRAINS_AUTHOR || '')
}

/** 仅用于提示的建议值，不会自动写入。 */
export function suggestedAuthor() {
  if (process.platform === 'win32') return 'win:local'
  if (typeof process.getuid === 'function') return `uid:${process.getuid()}`
  return ''
}

export function detectedIdentity(env = process.env) {
  return { machine: detectMachine(), os: detectOs(), author: detectAuthor(env) }
}

function hasAll(values) {
  return IDENTITY_FIELDS.every((field) => Boolean(values?.[field]))
}

function readState(file, logger) {
  if (!existsSync(file)) return null
  try {
    const parsed = JSON.parse(readFileSync(file, 'utf8'))
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('根节点不是对象')
    }
    return parsed
  } catch (error) {
    const backup = `${file}.corrupt-${Date.now()}`
    try {
      renameSync(file, backup)
      logger?.warn?.(`[atbrains] 身份文件损坏，已备份到 ${backup}：${String(error?.message || error)}`)
    } catch {
      logger?.warn?.(`[atbrains] 身份文件损坏且无法备份：${String(error?.message || error)}`)
    }
    return null
  }
}

function writeState(file, state, logger) {
  let tmp = ''
  try {
    mkdirSync(dirname(file), { recursive: true, mode: 0o700 })
    tmp = `${file}.tmp-${process.pid}-${Date.now()}`
    writeFileSync(tmp, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 })
    renameSync(tmp, file)
    return true
  } catch (error) {
    if (tmp) {
      try {
        unlinkSync(tmp)
      } catch {
        /* 临时文件可能根本没写出来 */
      }
    }
    logger?.warn?.(`[atbrains] 身份写入失败（本次仅内存生效）：${String(error?.message || error)}`)
    return false
  }
}

function displayPath(file) {
  const home = homedir()
  if (file === home) return '~'
  if (file.startsWith(`${home}${sep}`)) return `~${sep}${relative(home, file)}`
  return file
}

function safeField(field, raw, logger) {
  if (raw === undefined || raw === null || raw === '') return ''
  try {
    return cleanValue(field, raw)
  } catch (error) {
    logger?.warn?.(`[atbrains] 忽略无效的 ${field}：${String(error?.message || error)}`)
    return ''
  }
}

/**
 * 创建身份控制器。
 *
 * @param {object} opts
 * @param {object} [opts.config] 插件配置（machine/os/author 作为初始值）
 * @param {string} [opts.pluginVersion] 当前插件版本（用于识别安装/升级）
 * @param {object} [opts.logger] dsh logger（warn 不能抛）
 * @param {object} [opts.env] 环境变量（测试注入）
 * @param {string} [opts.file] 状态文件路径（默认 $DSH_HOME/atbrains/identity.json）
 * @param {() => Date} [opts.now] 时钟（测试注入）
 */
export function createIdentityController({
  config = {},
  pluginVersion = '0.0.0',
  logger,
  env = process.env,
  file = '',
  now = () => new Date(),
} = {}) {
  const statePath = resolve(expandHomePath(String(file || defaultIdentityFile(env))))
  const detected = detectedIdentity(env)
  const configured = {
    machine: safeField('machine', config?.machine, logger),
    os: safeField('os', config?.os, logger),
    author: safeField('author', config?.author, logger),
  }
  const configComplete = hasAll(configured)
  const saved = readState(statePath, logger)
  const firstInstall = !saved
  const versionChanged = Boolean(saved) && String(saved.pluginVersion || '') !== String(pluginVersion)

  const values = {}
  const origins = {}
  for (const field of IDENTITY_FIELDS) {
    const stored = safeField(field, saved?.[field], logger)
    if (stored) {
      values[field] = stored
      origins[field] = 'saved'
    } else if (configured[field]) {
      values[field] = configured[field]
      origins[field] = 'config'
    } else if (detected[field]) {
      values[field] = detected[field]
      origins[field] = 'detected'
    } else {
      values[field] = ''
      origins[field] = 'empty'
    }
  }

  // 配置文件把三项都写全 = 用户已经显式设置，不打扰；否则首次安装必须确认。
  let confirmed = Boolean(saved?.confirmed) || configComplete
  if (!hasAll(values)) confirmed = false
  let pending = !confirmed

  const createdAt = saved?.createdAt || nowIso(now)
  const state = {
    schemaVersion: IDENTITY_SCHEMA,
    pluginVersion: String(pluginVersion),
    machine: values.machine,
    os: values.os,
    author: values.author,
    confirmed,
    createdAt,
    updatedAt: nowIso(now),
  }
  if (firstInstall) state.installedAt = createdAt

  const changed =
    !saved ||
    versionChanged ||
    IDENTITY_FIELDS.some((field) => String(saved?.[field] || '') !== values[field]) ||
    Boolean(saved?.confirmed) !== confirmed
  if (changed) writeState(statePath, state, logger)

  function snapshot() {
    const missing = IDENTITY_FIELDS.filter((field) => !values[field])
    return {
      machine: values.machine,
      os: values.os,
      author: values.author,
      origins: { ...origins },
      confirmed,
      pending,
      missing,
      pluginVersion: String(pluginVersion),
      firstInstall,
      versionChanged,
      file: displayPath(statePath),
    }
  }

  function persist() {
    const updated = {
      ...state,
      machine: values.machine,
      os: values.os,
      author: values.author,
      confirmed,
      updatedAt: nowIso(now),
    }
    Object.assign(state, updated)
    return writeState(statePath, state, logger)
  }

  /**
   * 合并一次修改。只传一个字段也可以：其余字段保留当前值（检测值/配置值）。
   * @returns {{ok:true, changed:string[], identity:object}}
   */
  function set(patch = {}) {
    const changedFields = []
    const next = { ...values }
    for (const [rawKey, rawValue] of Object.entries(patch)) {
      const field = KEY_ALIASES[String(rawKey).trim().toLowerCase()] || (IDENTITY_FIELDS.includes(rawKey) ? rawKey : null)
      if (!field) throw new Error(`未知身份字段：${rawKey}`)
      const value = cleanValue(field, rawValue)
      if (value !== next[field]) changedFields.push(field)
      next[field] = value
    }
    if (!changedFields.length) {
      return { ok: true, changed: [], identity: snapshot(), message: '没有需要修改的值。' }
    }
    Object.assign(values, next)
    for (const field of changedFields) origins[field] = 'saved'
    confirmed = hasAll(values)
    pending = !confirmed
    persist()
    const identity = snapshot()
    const missing = identity.missing
    const message = missing.length
      ? `已保存，但仍缺少：${missing.map((f) => FIELD_LABELS[f]).join('、')}。`
      : '已保存并确认本机身份。'
    return { ok: true, changed: changedFields, identity, message }
  }

  /** 清掉保存过的值，回到配置/自动检测，并重新进入待确认状态。 */
  function reset() {
    for (const field of IDENTITY_FIELDS) {
      if (configured[field]) {
        values[field] = configured[field]
        origins[field] = 'config'
      } else if (detected[field]) {
        values[field] = detected[field]
        origins[field] = 'detected'
      } else {
        values[field] = ''
        origins[field] = 'empty'
      }
    }
    confirmed = configComplete
    pending = !confirmed
    persist()
    return { ok: true, identity: snapshot(), message: '已重置为自动检测值，等待重新确认。' }
  }

  return {
    file: statePath,
    displayFile: displayPath(statePath),
    firstInstall,
    versionChanged,
    get: snapshot,
    notice: () => setupNotice(snapshot()),
    set,
    reset,
    _detected: detected,
    _configured: configured,
  }
}

/** 首次对话注入的初始化说明（会被 pre-step 作为 source.form='setup' 的 user 消息插入）。 */
export function setupNotice(identity) {
  const escape = (value) => String(value ?? '').replace(/<\/system-reminder>/gi, '<\\/system-reminder>')
  const missing = identity.missing.length
    ? identity.missing.map((field) => FIELD_LABELS[field]).join('、')
    : '三项值都在，但还没有确认'
  const suggestion = identity.author || suggestedAuthor()
  return [
    '<system-reminder>',
    `[atbrains 初始化] 本机 atbrains 身份尚未确认（插件 ${escape(identity.pluginVersion)}${identity.firstInstall ? '，首次安装' : identity.versionChanged ? '，刚升级' : ''}）。`,
    `当前建议值：machine=${escape(identity.machine) || '（空）'}；os=${escape(identity.os) || '（空）'}；author=${escape(identity.author) || `（空，建议 ${escape(suggestion) || 'win:local / uid:<n>'}）`}`,
    `待确认/缺少：${missing}`,
    '请先向用户确认这三项（机器名、操作系统、作者/人类标识），确认后调用工具 atbrains_identity（action="set"，带 machine/os/author）保存。',
    '用户也可以直接输入 /atbrains set machine=<机器名> os=<系统> author=<标识> 保存；/atbrains 查看当前值，/atbrains reset 重新检测。',
    '或者在 cordis.patch.yml 里把 machine / os / author 三项写全（视为已确认）。',
    '字段限制：machine ≤48、os ≤64、author ≤64；值里不要换行。',
    '保存前不要把本条消息复述给用户，也不要把它当成用户的回答。',
    '</system-reminder>',
  ].join('\n')
}

/** 一行 tokenizer：支持单双引号，其他字符原样保留（中文值不需要转义）。 */
function tokenize(input) {
  const tokens = []
  let current = ''
  let quote = ''
  let started = false
  for (const ch of input) {
    if (quote) {
      if (ch === quote) {
        quote = ''
      } else {
        current += ch
      }
      started = true
      continue
    }
    if (ch === '"' || ch === "'") {
      quote = ch
      started = true
      continue
    }
    if (/\s/.test(ch)) {
      if (started || current) {
        tokens.push(current)
        current = ''
        started = false
      }
      continue
    }
    current += ch
    started = true
  }
  if (quote) return { error: '引号没有闭合' }
  if (started || current) tokens.push(current)
  return { tokens }
}

function normalizeKey(raw) {
  const key = String(raw || '')
    .trim()
    .toLowerCase()
    .replace(/[\s_-]+/g, '')
  return KEY_ALIASES[key] || null
}

/**
 * 解析 `/atbrains` 的自由输入。
 *   ''|'show'|'status'        → show
 *   'help'|'?'                → help
 *   'reset'|'clear'           → reset
 *   'set machine=... os=...'  → set（也接受 `机器=...`、`os: Windows 11`）
 *   省略 `set` 也可以：`/atbrains machine=r9000p author=win:local`
 * @returns {{action:'show'|'help'|'reset'}|{action:'set',patch:object}|{action:'invalid',error:string}}
 */
export function parseIdentityInput(rawInput) {
  const input = String(rawInput ?? '').trim()
  if (!input) return { action: 'show' }
  const head = input.split(/\s+/)[0].toLowerCase()
  if (head === 'help' || input === '?') return { action: 'help' }
  if (head === 'setup' || head === 'init' || head === 'wizard' || head === '向导') return { action: 'help' }
  if (head === 'reset' || head === 'clear') return { action: 'reset' }
  if (head === 'show' || head === 'status') return { action: 'show' }

  const body = head === 'set' ? input.slice(head.length) : input
  if (!body.trim()) return { action: 'show' }

  const { tokens, error } = tokenize(body)
  if (error) return { action: 'invalid', error }

  const patch = {}
  const positional = []
  let currentKey = ''
  for (const token of tokens) {
    // `key=value` 永远按字段处理；`key:value` 只有在 key 已知时才当字段，
    // 否则 `uid:0` / `http://…` 这类带冒号的值会被误判成未知字段。
    const equalAt = token.indexOf('=')
    const colonAt = token.indexOf(':')
    let key = ''
    let value = ''
    if (equalAt > 0) {
      key = normalizeKey(token.slice(0, equalAt))
      value = token.slice(equalAt + 1)
      if (!key) return { action: 'invalid', error: `未知字段：${token.slice(0, equalAt)}` }
    } else if (colonAt > 0) {
      const candidate = normalizeKey(token.slice(0, colonAt))
      if (candidate) {
        key = candidate
        value = token.slice(colonAt + 1)
      }
    }
    if (key) {
      patch[key] = value
      currentKey = key
      continue
    }
    if (currentKey) {
      patch[currentKey] = `${patch[currentKey]} ${token}`.trim()
    } else {
      positional.push(token)
    }
  }

  const unfilled = IDENTITY_FIELDS.filter((field) => !(field in patch))
  for (const token of positional) {
    const field = unfilled.shift()
    if (!field) break
    patch[field] = token
  }
  if (!Object.keys(patch).length) {
    return { action: 'invalid', error: '没有可设置的字段（例：set machine=r9000p os="Windows 11" author=win:local）' }
  }
  for (const [field, value] of Object.entries(patch)) {
    try {
      patch[field] = cleanValue(field, value)
    } catch (err) {
      return { action: 'invalid', error: String(err?.message || err) }
    }
  }
  return { action: 'set', patch }
}

function originText(origin) {
  return ORIGIN_LABELS[origin] || origin || ''
}

/** `/atbrains` 的纯文本输出（命令结果直接渲染，不进模型历史）。 */
export function renderIdentity(identity = {}) {
  const origin = (field) => {
    const label = originText(identity.origins?.[field])
    return label ? `  [${label}]` : ''
  }
  const missingFields = Array.isArray(identity.missing) ? identity.missing : []
  const missing = missingFields.length
    ? missingFields.map((field) => FIELD_LABELS[field] || field).join('、')
    : '无'
  return [
    'atbrains 本机身份',
    `  machine  ${identity.machine || '（空）'}${origin('machine')}`,
    `  os       ${identity.os || '（空）'}${origin('os')}`,
    `  author   ${identity.author || '（空）'}${origin('author')}`,
    `  状态     ${identity.pending ? `待设置（缺少：${missing}）` : '已确认'}`,
    `  插件     ${identity.pluginVersion}${identity.firstInstall ? '（首次安装）' : identity.versionChanged ? '（升级后检查）' : ''}`,
    `  文件     ${identity.file}`,
    '',
    '用法：/atbrains set machine=<机器名> os=<系统> author=<标识>',
    '      /atbrains reset    重新检测默认值并重新确认',
  ].join('\n')
}

export const IDENTITY_USAGE = '用法：/atbrains [show|set machine=<机器名> os=<系统> author=<标识>|reset]'
