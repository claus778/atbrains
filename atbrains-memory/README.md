# atbrains — Redis（短期）+ MariaDB（长期）记忆插件

Hermes 记忆提供者插件：把两台本机服务接进 agent 大脑。

## 分层设计

| 层 | 服务 | 内容 | 生命周期 |
|---|---|---|---|
| 短期 | Redis `127.0.0.1:9079` | `brain:recent` 最近对话流（Stream）、`brain:session:*` 会话状态、`brain:hot:*` 热点记忆镜像、`brain:qcache:*` 检索缓存 | TTL 自动过期（默认 14-30 天），无需维护 |
| 长期 | MariaDB `127.0.0.1:57306` / 库 `agent_memory` | `brain_memories` 事实/偏好/决策、`brain_claims`/`brain_claim_versions`/`brain_claim_evidence` 结构化主张与证据、`brain_episodes` 每轮情节头、`brain_turns` 完整对话归档、`brain_turn_messages` 全量转录层（工具调用/输出/推理，zlib）、`brain_sessions` 会话索引、`brain_archive` 压缩前存档 | 永久，`hermes atbrains-memory prune` 按保留期清理 |

## 接入点（Hermes 钩子）

- `prefetch` / `queue_prefetch` — 每轮对话前从长期库召回相关记忆 + 历史对话（结果缓存进 Redis，10 分钟内同样的查询直接命中）。
- `sync_turn` — 每轮对话完成：写入 Redis 对话流 + MariaDB 归档 + 会话计数；`messages` 参数（Hermes 传的完整消息链）增量写入 `brain_turn_messages`。
- `on_memory_write` — 内置 MEMORY.md / USER.md 的每次写入同步镜像到双存储。
- `on_pre_compress` — 上下文压缩前，将即将被丢弃的消息先存档进 `brain_archive`。
- `on_session_end` / `on_session_switch` — 会话边界维护。
- `on_delegation` — 子代理任务结果入库（kind=delegation）。

## 工具

`atbrains_remember` · `atbrains_recall` · `atbrains_forget` · `atbrains_recent` · `atbrains_stats` · `atbrains_turn_detail`
· `atbrains_claim_remember` · `atbrains_claim_recall` · `atbrains_episode_set` · `atbrains_episode_get`

## 知识层（schema v6：主张 / 版本 / 证据 / 情节）

`brain_memories` 是扁平文本（一段话 + kind/tags/importance），适合记「结论」；
需要「可核对、可回溯、会随时间变化」的知识时用这一层：

- `brain_claims` —— 一条主张的身份：`subject` / `predicate` / `kind` / `slot_key`（md5(machine|subject|predicate|kind)，唯一）
  + `current_revision` + `state`（active/retracted）。同一槽位再写只递增版本，不新增行。
- `brain_claim_versions` —— 每次写入的版本：`value_text`、`basis`（direct_report/observed/derived_summary/inferred_suggestion）、
  `valid_from`/`valid_to`（事实有效期）、`recorded_from`/`recorded_to`（记录期）、`replaces_revision`、
  `source_turn_id`/`source_seq`/`quote`（证据位置与引用）、`confidence`。
- `brain_claim_evidence` —— 证据链：(claim_id, revision) → (turn_id, seq)，`relation` 为 supports/contradicts/derived_from；
  指向 `brain_turn_messages` 的具体一条消息，可回读原文。
- `brain_episodes` —— 每轮一个情节头（主键 turn_id）：`goal`、`decisions`、`open_items`、`blockers`、`next_step`、`status`（open/done/abandoned）。

写入与读取：

```
atbrains_claim_remember(subject="服务器", predicate="时区", value="UTC",
                     turn_id=750, seq=2, quote="库内时间一律 UTC")
atbrains_claim_recall(subject="服务器")          # 当前值
atbrains_claim_recall(query="时区", history=true) # 全部版本（含已封版/已撤回）
atbrains_episode_set(turn_id=750, goal="…", decisions=[…], open_items=[…], next_step="…")
atbrains_episode_get(turn_id=750)
```

`atbrains_turn_detail` 会自动带出该轮的 episode；`atbrains_recall` 的返回里多一个 `claims` 字段。
`prune` 删轮次时，连带删该轮的 episode 与证据行（主张版本保留，只把断掉的 `source_turn_id` 置空）。

## 全量转录层（brain_turn_messages）

`brain_turns` 只存「用户说了什么 + 助手最终回复」，工具调用参数、工具返回原文、推理过程不在里面。
需要复盘某一轮到底跑了什么命令、返回了什么时，用这一层：

- 写入：`sync_turn` 拿到 Hermes 传来的 `messages`（OpenAI 风格完整消息链，已由 Hermes 做过密钥脱敏），
  每条一行：`role / tool_name / tool_call_id / raw_bytes / body(zlib json) / ts`。
- 增量：Hermes 每轮传的是「到目前为止的完整对话」，已归档条数记在
  `brain_sessions.archived_messages`，只写尾部新增，避免 N² 重复；历史被压缩/回退（条数变少）时
  退回最后一个 `user` 消息重新起算。
- 读取：`atbrains_turn_detail(turn_id=… | session_id=…)`，`max_chars` 控制每条正文上限；
  `brain_turns.message_count / message_bytes` 可先看这轮存了多少。
- 上限：单条 `turn_msg_max_bytes`（默认 1 MiB，超出截断并留 `_truncated_bytes`），
  单轮 `turn_total_max_bytes`（默认 16 MiB，超出部分下一轮续存）。
- 开关：`turn_messages_enabled`（默认 1）；`prune` 删轮次时按同一条时间线级联删转录。
- 召回端永远不返回这一层的内容（不参与向量、不进提示词），只能显式按 turn_id 取，避免工具输出挤占召回窗口。

## CLI

```
hermes atbrains-memory status            # 连通性 / 配置 / 统计总览
hermes atbrains-memory search <关键词>   # 检索记忆与历史对话
hermes atbrains-memory remember <内容>   # 手工存一条长期记忆
hermes atbrains-memory recent            # Redis 最近对话流
hermes atbrains-memory forget <id>       # 废止记忆（软删除）
hermes atbrains-memory prune --dry-run   # 清理预演
hermes atbrains-memory doctor            # 自检
hermes atbrains-memory remote-check      # 远程模式自检（开关 / URL / 令牌掩码 / 健康检查往返耗时）
```

## 配置

- 非密配置：`$HERMES_HOME/atbrains.json`（host/port/user/db + 行为参数，见 `core.py` DEFAULT_CONFIG）。
- 密码：`$HERMES_HOME/.env` 中 `ATBRAINS_REDIS_PASSWORD` / `ATBRAINS_MARIADB_PASSWORD`（经 `save_env_value` 写入）。
- 激活：`memory.provider: atbrains`（`hermes config set memory.provider atbrains`）。
- 写入方身份：`machine`（缺省 = 主机名）与 `agent`（缺省 `hermes`，≤32 字符、小写）
  决定 `brain_memories` / `brain_claims` / `brain_episodes` 的来源标注。
  同机多 agent 时可在 `atbrains.json` 写 `{"agent": "omp"}`；
  `/v1` 客户端由服务端令牌名 `<machine>|<agent>` 决定，客户端传值一律忽略。
  查询分布：`SELECT agent,machine,COUNT(*) FROM brain_memories GROUP BY agent,machine`。
- 多 agent 分库布局与容器重建命令：`/root/agent-db/`（credentials.txt 台账 / rebuild.sh / init.sql）。

## Python 依赖（重要设计决策）

`redis-py` 与 `PyMySQL` 是两个纯 Python 包，**已直接内置在插件 `vendor/` 目录**，
由 `core.py` 在导入时按需注入 `sys.path`（环境里已有安装则优先使用已安装版）。

**为什么不走 PM 的 `pip_dependencies` 声明**：PM 把声明了依赖的插件并入 uv workspace；
在本服务器的腾讯内网 PyPI 镜像下，重解析会因镜像缺少 PEP 700 `upload-time` 元数据
（uv 的 exclude-newer 隔离校验需要）而失败，进而触发 PM 的插件驱逐逻辑——
把插件写进 `plugins.disabled` 并清空 `memory.provider`。改用 vendor 后插件不参与
PM 构建，`hermes update` / 依赖重建均不受影响。

升级内嵌依赖：`pip download redis PyMySQL -d /tmp/w && python3 -m zipfile -e /tmp/w/<whl> vendor/`。

## 远程模式（Memory Service 客户端）

默认是**本地直连**（进程内直接读写 Redis + MariaDB）。若同网有另一台机器跑着
Memory Service（`atbrains` 插件，`/v1/*` 与网页控制台**共用同一个 HTTP 端口**，默认 8787），
可以把本机切成**远程模式**：召回/写入走 HTTP 调服务端，由服务端统一读写权威库。

```
hermes atbrains-memory remote-check        # 自检：开关 / URL / 令牌掩码 / GET /v1/health 往返耗时
hermes atbrains-memory remote-check --json
```

### 配置（`$HERMES_HOME/atbrains.json`，全部默认关闭）

| 键 | 默认 | 含义 |
|---|---|---|
| `remote_enabled` | `0` | 总开关。`0` 时**远程代码一行都不执行**，行为与本地直连逐字节一致 |
| `remote_url` | `""` | 服务端地址，例 `http://111.230.71.79:8787`（`/v1` 与 console 共用端口） |
| `remote_timeout_ms` | `800` | 关键路径（每轮 `prefetch` 召回）超时。必须短：绝不允许阻塞对话 |
| `remote_fallback` | `1` | 远程失败是否回落本地；`0` = 明确失败（召回返回空、写入返回 `id=None`），**不静默写本地** |
| `remote_turn_sync` | `0` | 对话归档（`sync_turn`）是否也走远程；默认关 = 归档仍写本地 |

```json
{ "remote_enabled": 1, "remote_url": "http://111.230.71.79:8787", "remote_timeout_ms": 800 }
```

### 令牌放 .env，不放 atbrains.json

- 文件：`$HERMES_HOME/.env`，变量名 `ATBRAINS_REMOTE_TOKEN`（可带引号，逐行解析、自动去引号）。
- 签发：在服务端机器上 `hermes atbrains token-add <machine>`（只显示一次），删令牌 `hermes atbrains token-rm`。
- 读取顺序：`cfg["remote_token"]`（仅调试用）→ `$HERMES_HOME/.env` 的 `ATBRAINS_REMOTE_TOKEN`。
- 为什么：`atbrains.json` 是非密配置（常被抄进版本库/同步盘/工单）；`.env` 与 MariaDB 密码同处、权限更紧。
  令牌**只用于请求头** `Authorization: Bearer <令牌>`，**绝不写回配置、绝不进日志、CLI 只打印掩码**（如 `tok_…`）。
- 客户端**不传 machine**：服务端用令牌反查 machine，写库时自动打上该机器的来源标记。

### 失败回落行为（fail-open）

| 路径 | 远程成功 | 远程失败 |
|---|---|---|
| `prefetch` 召回（关键路径） | 直接用服务端返回的注入文本，**不查本地 Redis 查询缓存** | `remote_fallback=1` → 记录 warning 后走原有本地召回；`=0` → 返回 `("", 0)` |
| `remember` 写入 | 服务端已写权威库 → **直接返回，不再写本地 MariaDB/Redis**（避免双写不一致） | `=1` → 回落本地写入；`=0` → 返回 `{"id": None, "error": ...}` |
| `forget` 废止 | 返回服务端 `result` | `=1` → 回落本地；`=0` → `deactivated=0` |
| `record_turn` 归档 | 仅当 `remote_turn_sync=1` 才走远程 | `=1` → 回落本地归档；`=0` → 返回 `0` |

任何情况下远程异常都不会抛给调用方：超时、连接失败、非 200、`ok != true` 一律按上表处理。
超时上限就是 `remote_timeout_ms`（默认 800ms），死地址也只会让该轮召回慢一个超时。

### 灰度建议（先给 server 自己开）

1. 服务端机器上确认 `hermes atbrains service` 自检通过、`/v1/health` 可访问。
2. **不**给服务端自己开远程模式（`remote_enabled` 保持 `0`）——服务端进程内直接读写库，
   开了会指向自己造成自调用。远端客户端才是要打开的对象。
3. 先在一台客户端上开：`{"remote_enabled": 1, "remote_url": ..., "remote_timeout_ms": 800}`，
   令牌写入该机 `.env` 后 `hermes atbrains-memory remote-check` 应显示 `ok ✓` 与毫秒级往返。
4. 观察一两轮对话：召回内容应来自服务端（同一份库）；`remote_fallback=1` 保证服务端抖动时
   该机自动回落本地，不会丢记忆也不会卡住对话。
5. 稳定后再逐台铺开；需要归档也集中到服务端时再开 `remote_turn_sync`。

## 维护与自动化（不依赖 systemd）

本插件的后台维护全部在**进程内**完成，不需要任何 systemd 单元或定时器：

- **向量补漏**：由进程内 drainer 巡检负责。长命进程（网关）每 `vector_drain_interval`
  （默认 60 秒）扫一次未向量化的记忆；短命 CLI 进程只在运行期间尽力处理，进程退出即结束，
  它遗留的欠账由后续任意长命进程补上。
- **清理（prune）**：由进程内维护循环负责。每 `maintenance_interval_min`（默认 60 分钟）
  检查一次，仅当距 `brain_meta.last_prune` 超过 `prune_every_days`（默认 7 天）才真正执行；
  执行前取 `GET_LOCK('brain_prune', 0)` 做多进程互斥，拿不到锁就跳过本轮（别的进程正在清）。
  完成后把 `last_prune`（UTC）写回 `brain_meta`，日志形如：

  ```
  brain: 维护清理完成 turns=0 sessions=0 archive=0 下次 7 天后
  ```

配置（`$HERMES_HOME/atbrains.json`，均有默认值）：

| 键 | 默认 | 含义 |
|---|---|---|
| `maintenance_enabled` | `1` | 维护循环总开关（`0` = 只保留手动清理） |
| `maintenance_interval_min` | `60` | 巡检间隔（分钟） |
| `prune_every_days` | `7` | 距上次清理超过该天数才真正执行（`0` = 每轮都清） |

维护线程与向量 drainer 一样是 `daemon=True`，**绝不阻塞进程退出**：短命 CLI 进程照常秒退。

**本插件不需要任何 systemd 单元。** 手动清理仍然可用：

```bash
python3 /root/agent-db/prune.sh
hermes atbrains-memory prune
hermes atbrains-memory prune --dry-run
```

## 结论蒸馏（distill）

把对话轮次自动提炼成「关于用户的长期结论」写回记忆库，默认**关闭**。

### 机制

```
brain_turns（水位之后的新轮次）
      │  攒够 distill_min_turns 轮 / 最老一条滞留超 distill_stagnation_hours
      ▼
brain-distill 后台线程（daemon，GET_LOCK('brain_distill') 互斥）
      │  LLM 提取（CommandCode API，复用 _http_json，UA hermes-brain/1.1）
      ▼
候选结论 ── 向量对账 ──┬─ 相似度 ≥ distill_high_sim  → 跳过（重复）
                      ├─ 相似度 <  distill_low_sim   → 新增
                      └─ 灰区 [low, high)            → 一次 LLM 统一裁决 skip/new/update
      │
      ▼
remember(source='distill') → brain_memories；update 时旧条标记 superseded_by
水位推进到本批最大 id（即使 0 条写入；手动 `--now` 与旧水位之间仍有积压轮次时保持原水位，
避免跳过），计数写回 brain_meta
```

- 线程与向量 drainer、维护循环同款：**惰性启动、daemon、进程内、零 systemd**，
  短命 CLI 进程照常秒退；`distill_enabled=0` 时线程根本不启动。
- 首次启用不会回灌历史：`distill_watermark` 缺失时只记当前 `MAX(brain_turns.id)` 后返回。
- 只处理 `ts` 早于 120 秒的轮次（给正在写入的对话留出落库窗口），跳过以 `[IMPORTANT` 开头的轮次。
- 任何异常只 warning + 计数，绝不向上抛、绝不影响记忆读写主路径。

### 配置（`$HERMES_HOME/atbrains.json`，均有默认值）

| 键 | 默认 | 含义 |
|---|---|---|
| `distill_enabled` | `0` | 总开关。`0` = 线程不启动，零行为变化、零 API 花费 |
| `distill_interval_min` | `60` | 巡检间隔（分钟）；首轮最多等 60 秒 |
| `distill_min_turns` | `8` | 攒够多少轮才蒸馏 |
| `distill_max_turns` | `16` | 单批最多送多少轮 |
| `distill_turn_chars` | `1000` | user / assistant 各自截取字符数 |
| `distill_stagnation_hours` | `24` | 不足 `min_turns` 但最老一条已滞留该小时数 → 也蒸馏 |
| `distill_max_items` | `6` | 单批最多产出多少条结论 |
| `distill_min_importance` | `4` | 产出 importance 下限（裁剪） |
| `distill_max_importance` | `7` | 产出 importance 上限（裁剪） |
| `distill_low_sim` | `0.85` | 低于该余弦相似度 → 新结论 |
| `distill_high_sim` | `0.94` | 达到该相似度 → 视为重复跳过；两者之间交 LLM 裁决 |
| `distill_api_base_url` | `https://api.commandcode.ai/provider/v1` | OpenAI 兼容 API 基址（实际请求 `{base}/chat/completions`） |
| `distill_api_model` | `deepseek/deepseek-v4.1-flash` | 蒸馏模型 |
| `distill_api_timeout` | `120` | 单次 LLM 请求超时（秒） |

API key 不进 `atbrains.json`：从环境变量 / `$HERMES_HOME/.env` 的
`HERMES_CUSTOM_API_COMMANDCODE_AI_API_KEY` 解析（与数据库密码同一机制）。
任何日志 / CLI 只报告「已配置 / 缺失」，绝不打印完整值。

### CLI

```bash
hermes atbrains-memory distill                    # 默认 = --status
hermes atbrains-memory distill --status           # 开关 / 水位 / 待蒸馏轮次 / 上次时间 / 五项计数 / API 配置
hermes atbrains-memory distill --dry-run          # 预演：默认最近 distill_max_turns 轮，真实提取+对账，零写入
hermes atbrains-memory distill --dry-run --turns 8
hermes atbrains-memory distill --now              # 手动强制跑一批并真实写入（无视 distill_enabled）
hermes atbrains-memory distill --now --turns 8
```

`--dry-run` 只读：不取锁、不写 MariaDB、不动水位、不累加计数；结尾固定打印
`未写库；水位未动`。`--now` 打印每条决策（`[new|skip|update]` + 相似度 + kind/importance
+ 内容截断）、写入的新记忆 id 列表与新水位。

### 启用步骤

1. 确认 `$HERMES_HOME/.env` 里有 `HERMES_CUSTOM_API_COMMANDCODE_AI_API_KEY`。
2. 在 `$HERMES_HOME/atbrains.json` 加 `{"distill_enabled": 1}`（可同时调整间隔/模型等）。
3. **需要新进程生效**：网关重启或开一个新会话；配置不会热加载。未显式打开时默认关闭。

### 成本与隐私

- 每轮巡检最多 2 次 LLM 调用（1 次提取 + 至多 1 次灰区对账）；按默认 60 分钟间隔，
  **每小时 ≤ 2 次**，且只有攒够轮次/出现灰区才真正调用。
- 隐私双保险：提示词明确禁止输出凭证/密钥/口令/token；落库前再用敏感正则
  （`sk-…`、`Bearer …`、`password/passwd/secret/token/密码/密钥/口令 = …`）兜底过滤，
  命中即丢弃该条并计入 `skipped`。发送给 LLM 的只是截断后的对话轮次，不会发送向量库内容。

## 设计说明

- 多设备共享：`brain_memories` / `brain_turns` / `brain_sessions` 均带 `machine` 列（来源机标识，取自 `atbrains.json` 的 `machine`，缺省=主机名）；`brain_memories` / `brain_claims` / `brain_episodes` 另有 `agent` 列（写入方标识，取自 `atbrains.json` 的 `agent`，缺省 `hermes`；`/v1` 客户端由令牌名决定，客户端不能覆盖）；召回片段自动标注来源。
- 检索使用 LIKE 词法匹配（中文按双字组切分），个人库规模下足够快；不依赖分词/嵌入服务。
- 写入路径失败只告警不抛出 —— 数据库暂不可达不阻塞对话。
- 库内时间戳统一按 UTC 解释（容器 `+00:00`；客户端不得按本机时区解读 naive DATETIME）。
