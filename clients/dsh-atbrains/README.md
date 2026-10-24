# dsh-atbrains 0.3.3 —— dsh 原生记忆插件

把 atbrains 共享长期记忆挂进 DeepSeek Harness（dsh）。**原生 Cordis 插件，纯 Node**：

- 不经 MCP JSON-RPC，不 spawn 子进程，不调用 Python 脚本 —— 只有 `fetch` 打 atbrains 的 `/v1` HTTP 接口；
- 每轮**自动召回**（不用模型自觉调工具）；
- 每轮**自动归档**：完整消息链（含工具调用/工具输出/推理）进 `brain_turns` + `brain_turn_messages`；
- 服务不可达/超时/401 一律静默降级，不阻塞对话。

```
dsh 回合 ──agent/pre-step──► GET /v1/memory/recall ──► 注入 source.form='recall' 消息
       └──agent/turn-stopping──► POST /v1/memory/turn ──► brain_turns / brain_turn_messages
```

## 装法（Windows / r9000p）

1. 解包到 `%USERPROFILE%\.dsh\plugins\dsh-atbrains`（或 `dsh plugin add ./dsh-atbrains`）。
2. 令牌放环境变量，不写进配置文件：

   ```powershell
   setx ATBRAINS_MCP_TOKEN "<你的令牌>"
   ```

   令牌名在服务端是 `r9000p-Windows|dsh`（左边=主机、右边=agent）。**令牌值沿用原来那个**，
   换名不影响你手上的令牌串。
3. `cordis.patch.yml` 里改 `baseUrl`（默认 `http://111.230.71.79:8787`）。
4. 重启 dsh。日志里应出现：

   ```
   [atbrains] 已启用 → http://111.230.71.79:8787（召回 + 自动归档）
   ```

> 0.2.0 及更早版本走 `/mcp`（JSON-RPC）且只做召回。0.3.0 起改走 `/v1`，并补上归档；
> 旧配置里的 `url: .../mcp` 仍然能被识别（会自动去掉 `/mcp` 后缀）。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `baseUrl` | `http://111.230.71.79:8787` | 服务地址；带 `/mcp` 或 `/v1` 都会归一化 |
| `tokenEnv` | `ATBRAINS_MCP_TOKEN` | 令牌所在环境变量名 |
| `token` | `''` | 直接给令牌（优先级更高，不建议落文件） |
| `timeoutMs` | `1200` | 召回超时 |
| `writeTimeoutMs` | `4000` | 归档写超时 |
| `maxChars` | `1200` | 注入文本上限 |
| `limit` | `5` | 召回条数上限 |
| `minScore` | `0.25` | 向量余弦低于它丢弃（无分值的命中保留） |
| `platform` | `dsh` | 写进 `brain_turns.platform` 的入口名（全小写，服务端触发器兜底） |
| `author` | `''` | 留空 = 由库侧按 `(machine, platform)` 规则回填（r9000p → `win:local`） |
| `archiveEnabled` | `true` | 关掉就只召回、不归档 |
| `archiveMaxChars` | `16000` | 单条消息保留上限（超长工具输出截断） |
| `archiveMaxMessages` | `400` | 每轮提交的消息条数上限（服务端硬上限 4000） |
| `showAgentTag` | `false` | 注入行是否标来源 agent |
| `protocolMajor` | `2` | 期望的 `/v1` 协议主版本；`health.version` 不一致就停用记忆 |

## 身份与库侧口径

- 服务端用令牌反查身份，客户端塞 `machine` 无效：令牌名 `主机|agent` → `machine='r9000p-Windows'`、`agent='dsh'`。
- `platform='dsh'` 是 dsh 自己的入口名（Hermes 的是 `hermes-cli` / `hermes-desktop`），库侧触发器会强制小写。
- 转录层按会话游标（`brain_sessions.archived_messages`）只写增量：同一份会话连发两次，第二次 0 条。
- 会话压缩/替换导致条数变少时，服务端退回最后一个 user 消息重新起算（不会重复灌）。

## 自测

```bash
node test/recall.mjs     # 召回：注入形状、去重、失败降级（离线）
node test/archive.mjs    # 归档：dsh Message → 转录层映射、游标、脱敏（离线）
node test/wiring.mjs     # 装配：apply() 挂载三个监听，服务不可达不抛（离线）
node test/protocol.mjs   # 协议护栏：主版本不匹配停用、探活失败不判死（离线）
node test/live.mjs       # 真机：health/status/recall/turn（需要 ATBRAINS_MCP_TOKEN）
```

`live.mjs` 只会写 `zz-dsh-plugin-live` 这个测试会话，跑完会打印清理 SQL。

## 协议与源码位置

- 契约文档：`/root/atbrains/docs/protocol-v1.md`（`/v1` 字段、错误码、版本策略）。
- 插件启动后**第一次**召回/归档前会 `GET /v1/health`，主版本不一致就停用并打 error ——
  避免服务端字段变了、客户端还照旧写。探活失败不判死（交给真实请求降级）。
- 源码在 `/root/atbrains/clients/dsh-atbrains`（git 管理，tag `dsh-atbrains-v0.3.1`），
  分发包在 `/root/agent-db/dist/`。

## 排障

| 现象 | 查什么 |
|---|---|
| 日志 `未找到令牌` | 环境变量没设 / 设完没重启 dsh（`setx` 只对新进程生效） |
| 报 `Cannot find package '@deepseek-ai/schemastery'` | dsh 没解析到 peer 依赖：在插件目录 `npm i @deepseek-ai/schemastery` 一次 |
| 日志 `召回失败 HTTP 401` | 令牌错或已被轮换（服务端 `hermes atbrains token-list`） |
| 日志 `召回失败 HTTP 404` | `baseUrl` 指到了别的服务（必须是 atbrains 的 8787） |
| 库里没有新行 | `SELECT platform,machine,author FROM brain_turns ORDER BY id DESC LIMIT 5;` 看 `platform='dsh'`；再看 `/var/log/atbrains-access.log` 有没有该 IP 的 `POST /v1/memory/turn` |
| 召回注入但没内容 | 服务端 `prefetch` 为空：确认库里有活跃记忆，且 `minScore` 没设太高 |
| 和 dsh-mnemon 同时装 | 两边都挂 `agent/pre-step`，会双注入；二选一 |
| `brain_turns.user_content` 是一段 `[atbrains 长期记忆] …` | 0.3.1 的 bug：归档把注入的召回块当成了用户提问（dsh 把上下文/skill 清单也塞成 user 消息）。升 0.3.2 |
| 会话超过 400 条后每轮 `message_count=0`（有 user_content、没转录） | 0.3.2 及更早只传尾部窗口却没带 `total`，服务端「窗口长度==游标」判断成没有增量。升 0.3.3（服务端同时要 ≥2.0.1） |

## 边界

- **不存系统提示**：`system` / `developer` 消息按项目约定丢弃；dsh 用 `user` 角色伪装的
  运行时上下文、skill 清单（`<system-reminder>`）以及本插件注入的召回块同样丢弃 ——
  只有真正由人输入的那条才作 `user_content`。
- **不写知识层**：claims / episodes 仍走 MCP 工具（`atbrains_claim_*`），本插件只管召回与转录。
- **不跨机同步对话**：归档只写本机会话，服务端按令牌识别主机。
- 密钥样式（`sk-…` / `Bearer …` / `AKIA…`）在入库前被抹成 `[redacted]`。
