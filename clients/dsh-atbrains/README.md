# dsh-atbrains 0.4.0 —— dsh 原生记忆插件

把 atbrains 共享长期记忆挂进 DeepSeek Harness（dsh）。**原生 Cordis 插件，纯 Node**：

- 不经 MCP JSON-RPC，不 spawn 子进程，不调用 Python 脚本 —— 只有 `fetch` 打 atbrains 的 `/v1` HTTP 接口；
- 每轮**自动召回**（不用模型自觉调工具）；
- 每轮**自动归档**：完整消息链（含工具调用/工具输出/推理）进 `brain_turns` + `brain_turn_messages`；
- 首次安装 / 升级后**必须确认本机身份**（machine / os / author），第一次对话自动提示，`/atbrains` 可随时重设；
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

## 首次安装 / 升级：身份设置（0.4.0）

插件第一次加载、或版本号变化后，会检查本机身份是否完整：

| 字段 | 来源 | 说明 |
|---|---|---|
| `machine` | 自动检测 `hostname`，可被配置/命令覆盖 | 机器名，≤48 字符 |
| `os` | 自动检测（Windows 按 build 号区分 10/11） | 操作系统，≤64 字符 |
| `author` | 不自动猜（只读 `ATBRAINS_AUTHOR`，可被配置/命令覆盖） | 作者/人类标识，≤64 字符，如 `win:local`、`uid:1000`、`wx:<openid>` |

- 状态写到 **`$DSH_HOME/atbrains/identity.json`**（默认 `~/.dsh/atbrains/identity.json`），
  **不写插件目录**（升级覆盖 `lib/` 不会丢），也不改 `cordis.patch.yml`。
- 首次安装 `author` 为空 → `setupPending`。**安装后的第一次对话**，pre-step 会注入一条
  `[atbrains 初始化]` 提示：模型先向你确认三项，再调用 `atbrains_identity` 工具保存；
  你也可以直接输入命令：

  ```
  /atbrains                                             # 查看当前值 / 是否待设置
  /atbrains set machine=r9000p os="Windows 11" author=win:local
  /atbrains set 机器=r9000p 系统="Windows 11" 作者=win:local   # 中文键名
  /atbrains reset                                       # 丢掉保存值，重新检测并重新确认
  ```

- 保存后 `pending=false`，后续升级不会再打扰；升级时若发现缺值（例如旧版本没有 `os`），
  会重新进入待设置，并在下一次对话提示。
- `machine` 仍以服务端令牌反查为准（`/v1` 不接受客户端伪造）；这里的 machine/os 用于本机
  身份展示与 `author` 归档，不会改变服务端归属。
- 不想被提示可设 `onboarding: false`；`/atbrains` 和 `atbrains_identity` 仍然可用。
- 兼容旧配置：`author` 仍可作为初始值；三项都在 `cordis.patch.yml` 里写全时视为已确认。

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
| `author` | `''` | 身份初始 author；确认后以 `identity.json` 为准。留空 = 库侧按 `(machine, platform)` 回填 |
| `machine` | `''` | 身份初始机器名；空 = 自动检测 hostname |
| `os` | `''` | 身份初始操作系统；空 = 自动检测 |
| `identityFile` | `''` | 身份状态文件；空 = `$DSH_HOME/atbrains/identity.json` |
| `onboarding` | `true` | 首次安装/升级缺值时，第一次对话是否注入身份设置提示 |
| `archiveEnabled` | `true` | 关掉就只召回、不归档 |
| `archiveMaxChars` | `16000` | 单条消息保留上限（超长工具输出截断） |
| `archiveMaxMessages` | `400` | 每轮提交的消息条数上限（服务端硬上限 4000） |
| `showAgentTag` | `false` | 注入行是否标来源 agent |
| `protocolMajor` | `2` | 期望的 `/v1` 协议主版本；`health.version` 不一致就停用记忆 |

## 身份与库侧口径

- 服务端用令牌反查身份，客户端塞 `machine` 无效：令牌名 `主机|agent` → `machine='r9000p-Windows'`、`agent='dsh'`。
- 本机 `identity.json` 的 `author` 确认后会随每轮归档发送；留空时仍由库侧
  `brain_author_defaults(machine, platform)` 回填（`r9000p-Windows` → `win:local`）。
- `identity.json` 含 `pluginVersion`：同版本重启不打扰；版本变化且三项齐全也不打扰，
  只有缺值/未确认才重新进入待设置。
- `platform='dsh'` 是 dsh 自己的入口名（Hermes 的是 `hermes-cli` / `hermes-desktop`），库侧触发器会强制小写。
- 转录层按会话游标（`brain_sessions.archived_messages`）只写增量：同一份会话连发两次，第二次 0 条。
- 会话压缩/替换导致条数变少时，服务端退回最后一个 user 消息重新起算（不会重复灌）。

## 自测

```bash
node test/recall.mjs     # 召回：注入形状、去重、失败降级（离线）
node test/archive.mjs    # 归档：dsh Message → 转录层映射、游标、脱敏（离线）
node test/identity.mjs   # 身份：首次安装/升级、set/reset、命令解析、文件损坏（离线）
node test/wiring.mjs     # 装配：apply() 挂载监听 + /atbrains + 工具，服务不可达不抛（离线）
node test/protocol.mjs   # 协议护栏：主版本不匹配停用、探活失败不判死（离线）
node test/live.mjs       # 真机：health/status/recall/turn（需要 ATBRAINS_MCP_TOKEN）
```

`live.mjs` 只会写 `zz-dsh-plugin-live` 这个测试会话，跑完会打印清理 SQL。

## 协议与源码位置

- 契约文档：`/root/atbrains/docs/protocol-v1.md`（`/v1` 字段、错误码、版本策略）。
- 插件启动后**第一次**召回/归档前会 `GET /v1/health`，主版本不一致就停用并打 error ——
  避免服务端字段变了、客户端还照旧写。探活失败不判死（交给真实请求降级）。
- 源码在 `/root/atbrains/clients/dsh-atbrains`（git 管理，tag `dsh-atbrains-v0.4.0`），
  分发包在 `/root/agent-db/dist/`。

## 排障

| 现象 | 查什么 |
|---|---|
| 日志 `未找到令牌` | 环境变量没设 / 设完没重启 dsh（`setx` 只对新进程生效） |
| 报 `Cannot find package '@deepseek-ai/schemastery'` | dsh 没解析到 peer 依赖：在插件目录 `npm i @deepseek-ai/schemastery` 一次 |
| 日志 `召回失败 HTTP 401` | 令牌错或已被轮换（服务端 `hermes atbrains token-list`） |
| 日志 `召回失败 HTTP 404` | `baseUrl` 指到了别的服务（必须是 atbrains 的 8787） |
| 日志 `身份待设置：缺少 author` | 第一次对话会提示；也可 `/atbrains set author=win:local`（或一次写全 machine/os/author） |
| 输入 `/atbrains` 没反应 | 当前 dsh 组合没挂 `commands`；改用 `atbrains_identity` 工具或直接改 `identity.json` |
| 归档 `author` 是 `unknown` | `identity.json` 的 author 为空且库侧没有该 machine 的回落规则：`/atbrains set author=…` |
| 库里没有新行 | `SELECT platform,machine,author FROM brain_turns ORDER BY id DESC LIMIT 5;` 看 `platform='dsh'`；再看 `/var/log/atbrains-access.log` 有没有该 IP 的 `POST /v1/memory/turn` |
| 召回注入但没内容 | 服务端 `prefetch` 为空：确认库里有活跃记忆，且 `minScore` 没设太高 |
| 和 dsh-mnemon 同时装 | 两边都挂 `agent/pre-step`，会双注入；二选一 |
| `brain_turns.user_content` 是一段 `[atbrains 长期记忆] …` | 0.3.1 的 bug：归档把注入的召回块当成了用户提问（dsh 把上下文/skill 清单也塞成 user 消息）。升 0.3.2 |
| 会话超过 400 条后每轮 `message_count=0`（有 user_content、没转录） | 0.3.2 及更早只传尾部窗口却没带 `total`，服务端「窗口长度==游标」判断成没有增量。升 0.3.3（服务端同时要 ≥2.0.1） |
| `brain_thoughts.ts` / `brain_tool_calls.started_at` 全是 NULL（`brain_turns.ts` 正常） | 0.3.3 及更早的转录没带消息级时间，服务端投影只能留空。升 0.3.4：每条记录带 `timestamp`（依次取 `timestamp/ts/at/time/createdAt`，消息上没有就用事件的 `at`） |

## 边界

- **不存系统提示**：`system` / `developer` 消息按项目约定丢弃；dsh 用 `user` 角色伪装的
  运行时上下文、skill 清单（`<system-reminder>`）以及本插件注入的召回块同样丢弃 ——
  只有真正由人输入的那条才作 `user_content`。
- **不写知识层**：claims / episodes 仍走 MCP 工具（`atbrains_claim_*`），本插件只管召回与转录。
- **不跨机同步对话**：归档只写本机会话，服务端按令牌识别主机。
- 密钥样式（`sk-…` / `Bearer …` / `AKIA…`）在入库前被抹成 `[redacted]`。
