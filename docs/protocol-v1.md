# atbrains `/v1` 协议（v2）

这份文档是**唯一的跨进程契约**：Hermes 侧（Python 插件 / 远程模式）和任何别的客户端
（dsh 原生插件、omp 未来的插件、外部脚本）都以这里为准。
`/mcp` 是并列的第二入口（JSON-RPC），工具签名另见 `atbrains_mcp.py`，语义相同。

## 传输与鉴权

- 基址：`http://<host>:8787`，前缀 `/v1`；`Content-Type: application/json`。
- 鉴权：`Authorization: Bearer <令牌>`（令牌在 `plugin/service-tokens.json`，600）。
  唯一例外：`GET /v1/health` 免令牌。
- 令牌决定身份，**客户端不能在 body 里指定 `machine` / `agent`**（防伪造）。
  令牌名规范写法 `<主机>|<agent>`：
  - `/v1`：左 = `machine`，右 = `agent`
  - `/mcp`：只取右 = `agent`，`machine` 恒为服务端主机
  - 不含 `|` 的老名字：`/v1` 名字即 `machine`、agent 固定 `hermes`；`/mcp` 名字即 agent
- 时间一律 UTC。字符集 utf8mb4。

## 版本

`GET /v1/health` 返回 `{ok, version, ts}`，`version` 是服务端协议版本（当前 `2.0.0`）。

- **主版本变更 = 破坏性**：客户端必须校验主版本，不一致就停用而不是静默降级
  （dsh 插件：`protocolMajor`，见 `clients/dsh-atbrains/lib/index.js` 的 `protocolOk()`）。
- 次版本/补丁 = 向后兼容的字段追加，客户端必须忽略未知字段。
- 契约变更流程：改 `service.py` → 同步本文件 → 跑 `scripts/contract-v1.py` → 同步客户端。
  只改服务端不改契约文档，视为未完成。

## 端点

| 方法 | 路径 | 入参 | 返回 |
|---|---|---|---|
| GET | `/v1/health` | — | `{ok, version, ts}` |
| POST | `/v1/memory/recall` | `{query, session_id?, limit?}` | `{ok, text, count, items[]}` |
| POST | `/v1/memory/remember` | `{content, kind?, tags?, importance?, memory_type?}` | `{ok, saved}` |
| POST | `/v1/memory/forget` | `{id}` | `{ok, result}` |
| POST | `/v1/memory/recent` | `{limit?}` | `{ok, hot[], turns[]}` |
| POST | `/v1/memory/turn` | `{session_id, user, assistant?, platform?, author?, messages?}` | `{ok, id}` |
| POST | `/v1/memory/status` | `{}` | `{ok, machine, agent, ping, stats}` |

### `recall`

- `text` 是**权威注入文本**（服务端 `prefetch_block` 的产物，已按重要度+时效+RRF 排序）；
  `items[]` 只是结构化命中，供展示/审计：`{id, kind, memory_type, importance, machine, agent,
  tags, via, vscore, preview}`。
- `vscore` 可能为 `null`（纯关键词命中）。客户端可据此过滤，但**不要自己重排**。

### `turn`（归档）

- `messages[]` 传**到目前为至的完整会话**，不是增量：服务端用
  `brain_sessions.archived_messages` 游标只写尾部新增部分，重复提交不会写重。
- 消息形状：`{role, content, tool_name?, tool_call_id?, tool_calls?, reasoning?, ts?}`，
  `content` 为字符串；`tool_calls` 为 `[{id, type:'function', function:{name, arguments}}]`。
  `system`/`developer` 不入库（客户端负责丢弃）。
- `assistant` 可为空（只有工具调用、没有可见回复的回合）。
- `platform` 走词表：`hermes-cli` / `hermes-desktop` / `weixin` / `telegram` / `discord` /
  `slack` / `email` / `cron` / `oneshot` / `subagent` / `unknown`，新平台小写透传（如 `dsh`）。
  库侧触发器会强制规范化，客户端写旧别名也不会污染库。
- `author` 留空 = 由库侧按 `brain_author_defaults(machine, platform)` 回填。
- 幂等性：同一 `session_id` + 同一份 `messages` 重复提交 → 第二轮 `message_count` 增量为 0。
  会话被压缩/替换导致条数变少时，服务端退回最后一个 `user` 消息重新起算（不会丢尾部）。

## 错误

统一 `{"ok": false, "error": "<脱敏后的原因>"}`：

| 状态 | 含义 | 客户端应有行为 |
|---|---|---|
| 400 | 参数不合法（缺 `query`/`session_id`/`user`，`messages` 不是数组…） | 不重试，修参数 |
| 401 | 令牌缺失/错误 | 不重试，查令牌 |
| 404 | 路径不存在 | 不重试，查 baseUrl |
| 500 | 服务端内部故障（已脱敏） | 可重试一次，然后降级为“无记忆” |

客户端不得因为记忆服务失败而中断对话：召回失败 → 跳过注入；归档失败 → 记日志、下次重试。

## 不被这条契约覆盖的

- 库表结构、Redis 键、Qdrant 集合：**服务端内部实现**，客户端不得直接访问数据库。
- 词表/作者规则：由库侧触发器强制，客户端只负责“尽量写对”。
- `brain_memories.agent` 之外的对话转录层没有 agent 列：`brain_turns` 只有 `platform`/`machine`/`author`。
