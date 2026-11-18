# atbrains `/v1` 协议（v2.1）

这份文档与 [`contracts/openapi.yaml`](../contracts/openapi.yaml) 是**唯一的跨进程契约**：
任意 Agent 客户端（Hermes 适配器、AstrBot 插件、dsh 插件、Python SDK、外部脚本）都只通过 HTTP 与 `atbrains-server` 交互，客户端不得直接连接 MariaDB / Redis / Qdrant。
`/mcp` 是并列的第二入口（JSON-RPC 2.0，工具注册表见 `server/atbrains_server/tools_registry.py`）。

## 传输与鉴权

- 基址：`http://<host>:8787`，前缀 `/v1`；`Content-Type: application/json`。
- 鉴权：`Authorization: Bearer <令牌>`（服务端令牌表位于 `<data_dir>/service-tokens.json`，权限 600）。
  唯一例外：`GET/POST /v1/health` 免令牌。
- 令牌决定身份，**客户端不能在 body 里伪造 `machine` / `agent`**。
  令牌名规范写法 `<主机>|<agent>`（例如 `laptop|codex`、`bot-host|astrbot`）：
  - `/v1`：左 = `machine`，右 = `agent`
  - `/mcp`：只取右 = `agent`，`machine` 恒为服务端主机
  - 不含 `|` 的历史令牌名：`/v1` 名字即 `machine`、agent 默认 `hermes`；`/mcp` 名字即 `agent`
  - 令牌角色 `role`：`agent`（常规读写/召回）| `admin`（允许调用 `/v1/admin/*` 与管理令牌）
- 时间一律 UTC。字符集 utf8mb4。

## 版本

`GET /v1/health` 返回 `{ok, version, server_version, ts}`，`version` 是协议版本（当前 `2.1.0`），`server_version` 是服务端版本（当前 `3.0.0`）。

- **主版本变更 = 破坏性**：客户端必须校验主版本（当前期望 `2`），不一致就停用。
- 次版本/补丁 = 向后兼容的端点/字段追加，客户端必须忽略未知字段。
- 契约变更流程：改 `server/atbrains_server/service.py` → 同步 `contracts/openapi.yaml` 与本文件 → 跑 `server/tests/test_service.py` 与 `ops/contract-v1.py` → 同步客户端。

## 端点一览

### 记忆核心 (`/v1/memory/*`)

| 方法 | 路径 | 入参 | 返回 |
|---|---|---|---|
| GET/POST | `/v1/health` | — | `{ok, version, server_version, ts}` |
| POST | `/v1/memory/recall` | `{query, session_id?, limit?, kind?, agent?, include_turns?, include_claims?, include_turn_items?}` | `{ok, text, count, items[], claims?, turns?}` |
| POST | `/v1/memory/remember` | `{content, kind?, tags?, importance?, memory_type?, source?, session_id?}` | `{ok, saved}` |
| POST | `/v1/memory/forget` | `{id}` | `{ok, result}` |
| POST | `/v1/memory/restore` | `{id}` | `{ok, result}` |
| POST | `/v1/memory/deactivate` | `{content}` | `{ok, deactivated}` |
| POST | `/v1/memory/recent` | `{limit?}` | `{ok, hot[], turns[]}` |
| POST | `/v1/memory/turn` | `{session_id, user, assistant?, platform?, author?, messages?, total?}` | `{ok, id, archived}` |
| POST | `/v1/memory/turn/detail` | `{turn_id?, session_id?, max_chars?}` | `{ok, found, turn, messages[], stored, episode?}` |
| POST | `/v1/memory/archive` | `{session_id, kind?, payload, platform?}` | `{ok, id}` |
| POST | `/v1/memory/session/touch` | `{session_id, platform?}` | `{ok}` |
| GET/POST | `/v1/memory/status` | `{light?}` | `{ok, machine, agent, role, ping, stats?}` |

### 知识层与交接 (`/v1/claim/*` · `/v1/episode/*` · `/v1/context` · `/v1/handoff`)

| 方法 | 路径 | 入参 | 返回 |
|---|---|---|---|
| POST | `/v1/claim/remember` | `{subject, predicate, value, kind?, basis?, valid_from?, valid_to?, turn_id?, seq?, quote?, confidence?, importance?, shared?}` | `{ok, claim}` |
| POST | `/v1/claim/recall` | `{query?, subject?, predicate?, limit?, history?}` | `{ok, claims[], count}` |
| POST | `/v1/claim/retire` | `{claim_id, reason?}` | `{ok, result}` |
| POST | `/v1/episode/set` | `{turn_id, goal?, decisions?, open_items?, blockers?, next_step?, status?, session_id?}` | `{ok, episode}` |
| POST | `/v1/episode/get` | `{turn_id?, session_id?}` | `{ok, found, ...}` |
| POST | `/v1/context` | `{focus?, limit?, include_turns?, include_episodes?, max_chars?, format?}` | `{ok, agent, memories[], claims[], open_episodes[], recent_turns[], counts, markdown?}` |
| POST | `/v1/handoff` | `{note, next_step?, open_items?, turn_id?, importance?, session_id?}` | `{ok, saved, episode?, hint}` |

### 服务端管理 (`/v1/admin/*`，需 `admin` 令牌或控制台 `X-Auth-Token`)

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/v1/admin/overview` | 存储统计 / ping / 向量覆盖 / 容器与 Qdrant 状态 |
| GET | `/v1/admin/memories` | 分页检索记忆列表 |
| GET | `/v1/admin/memory` | 单条记忆详情 (`?id=`) |
| GET | `/v1/admin/turns` | 分页检索对话归档 |
| GET | `/v1/admin/turn` | 单轮归档详情 (`?id=`) |
| GET | `/v1/admin/sessions` | 分页会话索引 |
| GET | `/v1/admin/search` | 管理侧混合检索 |
| POST | `/v1/admin/drain` | 触发向量回填 (`{force?, max_items?}`) |
| POST | `/v1/admin/doctor` | 服务端体检 |
| POST | `/v1/admin/prune` | 清理过期/旧数据 (`{dry_run?}`) |
| GET/POST/DELETE | `/v1/admin/tokens` | 令牌管理（列掩码 / 签发 / 吊销） |

## 离线降级规范（客户端）

客户端不得直接连接数据库作为降级路径。读路径失败时应静默跳过注入；写路径遇到瞬时网络错误或 5xx 时，使用本地有界 JSONL 离线队列（`atbrains_client.OfflineSpool`）暂存，待服务端恢复后按序重放（`/v1/memory/turn` 游标幂等、`/v1/memory/remember` 内容哈希去重）。

## 错误码

| 状态 | 含义 | 客户端行为 |
|---|---|---|
| 400 | 参数非法 | 不重试，进 deadletter 或报错 |
| 401 | 令牌缺失或无效 | 不重试，检查令牌 |
| 403 | 非 admin 令牌调 `/v1/admin/*` | 不重试 |
| 404 | 路径不存在 | 检查 baseUrl |
| 500 / 502 / 503 | 服务端故障 | 写请求入离线队列，读请求降级无记忆 |
