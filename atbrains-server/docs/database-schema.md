# atbrains 记忆库准则 + 表结构字典

> 适用库：MariaDB `agent_memory`（127.0.0.1:57306 / 111.230.71.79:57306，账号 `agent`）
> schema 版本：**v9**（记录在 `brain_meta.schema_version`）
> 快照（2026-10-25 19:2x，数字随时会变，以现场查询为准）：memories 397（活跃 313）· turns 1,138 ·
> turn_messages 20,293（tool 10,423 / assistant 8,867 / user 992 / session_meta 11）· thoughts 7,784（24.0 MB）·
> tool_calls 10,636（51 种工具，ok 10,624 / error 2 / pending 10）· sessions 323 · archive 5 ·
> vector_index 314 · author_defaults 8 · 库体积 144.8 MB
> 这份文档就是“记忆准则”的权威版本。改表、改口径、改保留策略，先改这里，再改代码。

---

## 0. 四层存储，各管一段

| 层 | 位置 | 存什么 | 权威性 | 跨机 |
|---|---|---|---|---|
| 运行时账本 | `~/.hermes/state.db`（SQLite，每台机一份） | 完整会话、工具调用/输出、推理、系统提示、压缩摘要、FTS | 本机权威 | ❌ 不同步 |
| 短期层 | Redis `:9079`（一机一库号） | 最近对话流、会话状态、热点镜像、检索缓存、查询向量缓存 | 可丢，自动重建 | ❌ 不共享 |
| 长期层 | MariaDB `agent_memory` | 记忆、对话归档、会话索引、压缩存档、知识层、思考/工具明细、向量索引标记 | **全局权威** | ✅ 全机共享 |
| 向量层 | Qdrant `:6333` 集合 `brain_memories` | 记忆正文的 2560 维向量（语义召回） | 派生物，可重建 | ✅ 共享同一集合 |

一句话：**state.db 回答“这台机器发生过什么”，MariaDB 回答“所有 agent 共同知道什么”。** 删 state.db 丢本机回放与回填来源；删 MariaDB 只丢跨机召回。

---

## 1. 记忆准则（写之前先读这节）

### 1.1 归属三列，别混

| 列 | 含义 | 谁能定 |
|---|---|---|
| `machine` | 来源主机（server / x260-debian / r9000p-Windows / gigabyte-debian / r730） | 服务端按令牌反查，客户端不能伪造 |
| `agent` | 写入方身份（hermes / omp / dsh / artex / codex） | 同一套令牌体系；`/v1` 取令牌名 `主机|agent` 的右半，`/mcp` 令牌名即 agent |
| `platform` | 入口（`hermes-cli` / `hermes-desktop` / `weixin` / `telegram` / `cron` / `oneshot` / `subagent` / `dsh` / `unknown`…） | 客户端上报，库侧触发器强制小写与别名归并 |

- 只有 `brain_memories` / `brain_claims` / `brain_claim_versions` / `brain_episodes` 有 `agent` 列；
  **对话层（turns / sessions / turn_messages）没有 agent**，只有 machine + platform + author。
- 跨机只同步“记忆”，不同步“对话记录”。别台轮次少不是故障，也不要替它回填。

### 1.2 三条红线

1. **改 `brain_memories` / `brain_vector_index` 必须显式带 `updated_at = updated_at`**（或显式写值），
   否则 `ON UPDATE CURRENT_TIMESTAMP` 会把全表年龄冲成当下，扰动时间衰减与“最新在前”的浏览口径。
2. **转录层 `ts` 用源消息时间**，不是入库时间；否则整表看起来只有导入那天的数据。
3. **停用记忆走 `atbrains_forget`（或 `store.forget()`），不要手写 `UPDATE active=0`** ——
   工具会同时删 Redis 热点镜像 `brain:hot:<id>` 并 `ZREM brain:hot:index`，手写 SQL 会把热点层留脏。

### 1.3 读的一侧

- 召回 = 关键词 LIKE + 向量 topk12 → RRF(k=60) → 时间衰减（只作用于 episodic 层）→ 过滤
  `active=1 AND superseded_by IS NULL`。`brain_turn_messages` / `brain_thoughts` / `brain_tool_calls`
  **永不进召回、不进向量**，只能按 turn_id / session_id 显式取（`atbrains_turn_detail`）。
- 浏览/列表用“最新在前”视图（`*_newest`），别直接扫裸表（InnoDB 按主键聚簇，默认是老在前）。

---

## 2. 库级约定

| 项 | 值 |
|---|---|
| 引擎 / 字符集 | InnoDB / `utf8mb4` + `utf8mb4_unicode_ci` |
| 时区 | **全库 UTC**（容器 `default-time-zone=+00:00`），比北京时间慢 8 小时 |
| 迁移 | `ensure_schema()` 启动时幂等执行 `_DDL` + `_MIGRATIONS[n]`，`GET_LOCK('brain_schema_migrate')` 串行化；**已落库的迁移块不可再改，只能 `SCHEMA_VERSION+1` 开新块** |
| 版本号 | `SELECT meta_value FROM brain_meta WHERE meta_key='schema_version'`（不是列名 `schema_version`） |
| 大小写 | 表名/列名全小写；platform/author 值全小写 |

---

## 3. 表字典

### 3.1 `brain_memories` —— 长期记忆（唯一一张“记忆本体”表）

**用途**：所有 agent 共享的事实 / 偏好 / 决策 / 笔记 / 内置画像镜像。所有检索都从这里出发。
**行数**：397（active 313 / 停用 84）；agent 分布 hermes 385 / omp 9 / dsh 3。
**写入方**：`atbrains_remember`、内置 MEMORY.md/USER.md 镜像钩子、蒸馏线程、MCP、CLI。
**保留**：活跃行永久；停用行 365 天后由 `inactive_keep_days` 清理。

| 列 | 类型 | 默认 | 含义 / 取值 |
|---|---|---|---|
| `id` | bigint unsigned AI | — | 主键；`brain_vector_index.mem_id`、`superseded_by` 都指向它 |
| `kind` | varchar(32) | `note` | 记忆类别：`note` / `fact` / `decision` / `insight` / `preference` / `builtin-memory`（实测 fact 97、decision 88、note 76、builtin-memory 65、insight 27、preference 22） |
| `memory_type` | varchar(16) | `''` | 分层，决定是否衰减：`profile`（内置画像，永不衰减）/ `knowledge`（fact·decision·insight·preference，永不衰减）/ `episodic`（note·todo，半衰期 45 天）；早期行可能为空 |
| `content` | mediumtext | — | 记忆正文（召回与向量化的对象），最长 16 MB |
| `tags` | varchar(255) | `''` | 逗号分隔标签，`LIKE` 检索与蒸馏标记（如 `distill`） |
| `importance` | tinyint | `5` | 1–10，CHECK 约束；召回同分时按它排序 |
| `source` | varchar(64) | `''` | 来源：`tool`（工具写入）/ `memory_write`（内置文件镜像）/ `distill`（蒸馏）/ `migration`（历史导入）/ `mcp` / `cli` / `backfill` / `standalone-test` |
| `session_id` | varchar(96) | `''` | 写入时所在会话（可空） |
| `machine` | varchar(48) | `''` | 来源主机 |
| `agent` | varchar(32) | `hermes` | 写入方身份（跨 agent 共享库的关键列） |
| `active` | tinyint | `1` | 1=有效，0=已停用（软删）；CHECK 0/1 |
| `superseded_by` | bigint unsigned | NULL | 被哪条新记忆取代（**不是停用**，active 仍为 1；可 `unsupersede` 回滚） |
| `superseded_at` | datetime | NULL | 取代时间 |
| `deactivated_at` | datetime | NULL | 停用时间，prune 按它计时 |
| `created_at` | datetime | now | 创建时间（UTC） |
| `updated_at` | datetime | now, ON UPDATE | 最后更新时间 —— **时间衰减与“最新在前”都靠它，别乱冲** |
| `content_hash` | char(32) STORED | 生成列 | `IF(active=1, MD5(content), NULL)`；唯一键 `uniq_active_content` 用它防重复，停用后变 NULL（允许多个 NULL，所以“先忘再记”不会撞键） |

**索引**：`idx_recall(active, importance, updated_at)`、`idx_active_updated`、`idx_agent(active, agent, updated_at)`、
`idx_kind`、`idx_supersede(active, superseded_by)`、`uniq_active_content(content_hash)`。

---

### 3.2 `brain_turns` —— 一轮对话（索引行，不含全文）

**用途**：每轮一行的“台账”：谁的、哪个入口、说了什么（截断版）、转录了多少条。
**行数**：1,138。**写入方**：Hermes `sync_turn`、dsh 插件 `/v1/memory/turn`、cron/subagent 自动归档。

| 列 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `id` | bigint unsigned AI | — | 轮次 id，被 turn_messages / thoughts / tool_calls / claims.evidence / episodes 引用 |
| `session_id` | varchar(96) | `''` | 所属会话（对应 `brain_sessions.session_id`） |
| `platform` | varchar(48) | `''` | 入口，词表见 §1.1；库侧触发器强制小写+别名归并 |
| `author` | varchar(64) | `''` | 人类/来源标识：`uid:<n>`、`win:local`、`wx:<openid>`、`sys:cron|oneshot|subagent`、`unknown`；空值由 BEFORE INSERT 触发器查 `brain_author_defaults` 回填 |
| `machine` | varchar(48) | `''` | 来源主机 |
| `truncated` | tinyint | `0` | 正文是否被截断（user/assistant 各 16,000 字上限） |
| `message_count` | smallint unsigned | `0` | 本轮在转录层实际落了多少条消息（用于对账） |
| `message_bytes` | int unsigned | `0` | 转录层原始字节合计（压缩前） |
| `user_content` | mediumtext | — | 用户这一轮的输入（≤16k 字符，脱敏后） |
| `assistant_content` | mediumtext | — | 助手这一轮的最终可见回复（≤16k 字符） |
| `ts` | datetime | now | 轮次时间（UTC）——**没有 created_at，别写错列名** |

**索引**：`idx_ts(ts)`、`idx_session_ts(session_id, ts)`。

---

### 3.3 `brain_turn_messages` —— 全量转录（权威原文）

**用途**：本轮完整消息链的逐条原文，`zlib(JSON)` 压缩存 `body`。工具调用、工具输出、推理、附件占位都在这里。
**行数**：20,293（tool 10,423 / assistant 8,867 / user 992 / session_meta 11）。**写入方**：`store_turn_messages()`。
**保留**：随 turns 永久（`turns_keep_days=0`）；prune 时先删本表再删 turns。

| 列 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `id` | bigint unsigned AI | — | 主键 |
| `turn_id` | bigint unsigned | — | → `brain_turns.id` |
| `seq` | smallint unsigned | `0` | 轮内顺序（游标增量写入时的连续序号） |
| `role` | varchar(16) | `''` | `user` / `assistant` / `tool` / `session_meta`（会话级元信息，非模型消息） |
| `tool_name` | varchar(64) | `''` | tool 消息的工具名（从列冗余，body 里可能没有） |
| `tool_call_id` | varchar(128) | `''` | tool 消息对应的调用 id —— **配对参数与结果的唯一依据** |
| `raw_bytes` | int unsigned | `0` | 压缩前字节数（容量统计用） |
| `body` | longblob | — | `zlib(JSON)`，键含 `content` / `reasoning` / `tool_calls` / `timestamp` / `finish_reason` / `message_uid` 等 |
| `ts` | datetime | now | **原消息时间**（UTC），不是入库时间 |

**索引**：`idx_turn_seq(turn_id, seq)`、`idx_tm_ts(ts)`。
**读取**：只能按 turn/session 取（`atbrains_turn_detail`），不参与召回、不进向量。

---

### 3.4 `brain_thoughts` —— 思考链投影（v9）

**用途**：从转录层抽出的 reasoning，一条 assistant 消息一行，可按轮次读“它是怎么想的”。
**行数**：7,784（约 24.0 MB 文本）。**写入方**：`_index_turn_details()`（`store_turn_messages()` 之后同步）。
**幂等键**：`uniq_thought(turn_id, seq)`。**保留**：随 turns。

| 列 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `id` | bigint unsigned AI | — | 主键 |
| `turn_id` | bigint unsigned | — | → `brain_turns.id` |
| `seq` | smallint unsigned | `0` | 对应转录层里的消息序号（思考链按它排序） |
| `message_uid` | varchar(96) | `''` | 源消息唯一 id（跨层定位用） |
| `reasoning` | mediumtext | — | 思考原文（≤200k 字符） |
| `reasoning_chars` | int unsigned | `0` | 字符数（统计/筛查超长思考） |
| `finish_reason` | varchar(32) | `''` | 该次生成的结束原因（`stop` / `tool_calls` / …） |
| `ts` | datetime | NULL | 原消息时间（UTC） |

**索引**：`uniq_thought(turn_id, seq)`、`idx_thought_ts(ts)`。

---

### 3.5 `brain_tool_calls` —— 工具调用明细投影（v9）

**用途**：一次工具调用一行：完整参数、完整结果、成功/失败、起止与耗时。回答“哪个工具慢/总失败/什么时候调的”。
**行数**：10,636（51 种工具；terminal 5,378、read_file 1,184、pwsh 691、write_file 688、patch 629）。
**状态**：ok 10,624 / error 2 / pending 10（pending = 调用发出但没等到结果，多为被中断的回合）。
**写入方**：同 `_index_turn_details()`；幂等键 `uniq_tool_call(turn_id, seq, call_index)`。

| 列 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `id` | bigint unsigned AI | — | 主键 |
| `turn_id` | bigint unsigned | — | → `brain_turns.id` |
| `seq` | smallint unsigned | `0` | 发起调用的 assistant 消息序号（结果先到时会记在结果消息的序号上） |
| `call_index` | smallint unsigned | `0` | 同一条消息里第几个调用（0 起） |
| `call_id` | varchar(128) | `''` | provider 给的调用 id；与 `brain_turn_messages.tool_call_id` 配对 |
| `tool_name` | varchar(64) | `''` | 工具名（terminal / read_file / patch …） |
| `args` | mediumtext | NULL | 调用参数原文（JSON 串，≤1 MB） |
| `result` | mediumtext | NULL | 工具返回原文（≤1 MB；超长截断，原文在转录层） |
| `result_chars` | int unsigned | `0` | 返回字符数 |
| `status` | varchar(16) | `pending` | `ok` / `error` / `pending` |
| `started_at` | datetime | NULL | 调用时间（assistant 消息时间戳，UTC） |
| `ended_at` | datetime | NULL | 结果到达时间（tool 消息时间戳，UTC） |
| `duration_ms` | int unsigned | NULL | 两者之差（含排队，不等于纯执行耗时） |

**索引**：`uniq_tool_call(turn_id, seq, call_index)`、`idx_tc_tool(tool_name)`、`idx_tc_status(status)`、`idx_tc_turn(turn_id, seq)`。

---

### 3.6 `brain_sessions` —— 会话索引

**用途**：一个会话一行的汇总；也是转录游标（`archived_messages`）的存放处。
**行数**：323。**写入方**：`record_turn()` 首轮 upsert、`touch_session()`（会话结束/切换）、`store_turn_messages()` 补行。
**保留**：365 天，且只删“无近期轮次”的行。

| 列 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `id` | bigint unsigned AI | — | 物理主键（按 `started_at` 赋 1..N，纯粹为聚簇顺序好看） |
| `session_id` | varchar(96) | — | 业务主键（唯一），`local-<sha>` / `session-<uuid>` 等 |
| `platform` | varchar(48) | `''` | 会话入口（触发器强制词表） |
| `machine` | varchar(48) | `''` | 来源主机 |
| `started_at` | datetime | now | 首次出现时间（UTC） |
| `last_active` | datetime | now | 最后活动；touch/record_turn 都会更新 |
| `turn_count` | int | `0` | 轮次计数（由 record_turn 自增；对账用，可能落后于实际行数） |
| `archived_messages` | int unsigned | `0` | **转录层游标**：已归档到第几条消息；客户端每次传“到目前为至的完整会话”，服务端只取尾部增量；条数变少（压缩/回退）时退回最后一个 user 消息重算 |
| `note` | varchar(512) | `''` | 备注（控制台/人工用） |

**索引**：`uniq_session_id(session_id)`、`idx_sessions_time(started_at)`。

---

### 3.7 `brain_archive` —— 压缩前存档

**用途**：**只在上下文压缩那一刻**写一条（`kind='precompress'`），把压缩前消息逐条截 2,000 字存下来。
它**不是**“清理老数据的地方”——prune 是直接 DELETE。
**行数**：5（历史压缩次数少，常年近空是正常的）。**保留**：365 天。

| 列 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `id` | bigint unsigned AI | — | 主键 |
| `session_id` | varchar(96) | `''` | 所属会话 |
| `machine` | varchar(48) | `''` | 来源主机 |
| `platform` | varchar(48) | `''` | 入口（触发器强制） |
| `kind` | varchar(24) | `precompress` | 存档类型（当前只有压缩快照） |
| `ts` | datetime | now | 存档时间（UTC） |
| `payload` | mediumtext | — | 快照正文（逐条截断后拼接/JSON，≤400k 字符） |

**索引**：`idx_archive(session_id, ts)`。

---

### 3.8 知识层：`brain_claims` / `brain_claim_versions` / `brain_claim_evidence` / `brain_episodes`

**用途**：把“X 的 Y 是 Z”这类可核对的主张做成带版本、带证据的结构；以及每一轮的目标/决策/未完成项。
四张表当前都是 0 行（能力在、还没积累），写入方 `atbrains_claim_remember` / `atbrains_episode_set`。

**`brain_claims`（主张身份，一个槽位一行）**

| 列 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `id` | bigint unsigned AI | — | 主张 id |
| `machine` / `agent` | varchar(48)/(32) | `''`/`hermes` | 归属主机 / 写入方 |
| `subject` | varchar(191) | — | 主语（如“服务器”“用户”） |
| `predicate` | varchar(96) | — | 谓词（如“磁盘布局”“首选模型”） |
| `kind` | varchar(24) | `fact` | `fact`/`preference`/`constraint`/`decision`/`procedure`/`intention`/`alias` |
| `slot_key` | char(32) | — | `md5(machine|subject|predicate|kind)`，唯一键：同槽位再写只递增版本 |
| `current_revision` | int unsigned | `1` | 当前版本号 |
| `state` | varchar(16) | `active` | `active` / `retracted`（撤回保留历史） |
| `importance` | tinyint | `5` | 1–10 |
| `created_at` / `updated_at` | datetime | now / ON UPDATE | 时间 |

**`brain_claim_versions`（每一版的值）**

| 列 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `claim_id` + `revision` | bigint + int | — | 复合主键 |
| `agent` | varchar(32) | `hermes` | **这一版**是谁写的（v8 加的，修“历史版本全显示最新作者”的歧义） |
| `value_text` | mediumtext | — | 值正文 |
| `basis` | varchar(16) | `direct_report` | `direct_report`/`observed`/`derived_summary`/`inferred_suggestion` |
| `valid_from` / `valid_to` | datetime | NULL | 事实有效期（世界时间） |
| `recorded_from` / `recorded_to` | datetime | now / NULL | 记录期：写新版时给旧版打 `recorded_to` |
| `replaces_revision` | int unsigned | NULL | 取代了哪一版 |
| `source_turn_id` / `source_seq` | bigint / smallint | NULL | 证据所在轮次与消息序号（轮次被 prune 时置 NULL，不删版本） |
| `quote` | varchar(2048) | `''` | 证据原文摘录 |
| `confidence` | tinyint unsigned | `80` | 0–100 |
| `created_at` | datetime | now | 写入时间 |

**`brain_claim_evidence`（证据链）**：`(claim_id, revision, turn_id, seq, relation)` 复合主键，
`relation` 默认 `supports`，`quote` 为原文摘录，`created_at` 记录时间。指向 `brain_turn_messages` 的具体一条消息。

**`brain_episodes`（每轮情节头）**：以 `turn_id` 为主键；
`session_id` / `machine` / `agent` 归属；`goal`（目标）、`decisions` / `open_items` / `blockers`（JSON 数组串）、
`next_step`、`status`（`open`/`done`/`abandoned`）、`created_at`/`updated_at`。

---

### 3.9 运维表

**`brain_meta`（键值账本）** —— 9 行：

| 键 | 当前值 | 含义 |
|---|---|---|
| `schema_version` | `11` | 结构版本 |
| `last_prune` | 2026-10-24 05:47:13 | 上次清理时间（维护线程按它判断是否到 7 天） |
| `distill_watermark` | `1130` | 蒸馏已处理到的最大 turn id |
| `last_distill` | 2026-10-24 17:48:53 | 上次蒸馏时间 |
| `distill_runs` / `_added` / `_updated` / `_skipped` / `_errors` | 37 / 180 / 7 / 42 / 3 | 蒸馏计数 |
| `graph_cursor` | `max(brain_memories.id)` | 图谱增量抽取游标（v11 新增，首次只对齐不回溯） |
| `last_graph_build` | 时间戳 | 上次增量图谱抽取时间（10 分钟节流） |

列：`meta_key`(PK) / `meta_value` / `updated_at`(ON UPDATE)。

**`brain_vector_index`（向量索引标记，314 行，全部 status=ok）**

| 列 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `mem_id` | bigint unsigned PK | — | → `brain_memories.id`（一对一） |
| `status` | varchar(16) | `ok` | `ok` / 失败态（失败行 30 分钟退避重试） |
| `model` | varchar(64) | `''` | 向量模型（当前 `Qwen/Qwen3-Embedding-4B`） |
| `dim` | smallint unsigned | `0` | 维度（2560） |
| `content_hash` | char(32) | NULL | 已向量化内容的 MD5，用于判断是否过期 |
| `error` | varchar(255) | `''` | 失败原因（脱敏） |
| `embedded_at` | datetime | NULL | 向量化时间 —— **由触发器强制 `UTC_TIMESTAMP()`**，不信进程本地时钟 |
| `updated_at` | datetime | now, ON UPDATE | 行更新时间（改这表必须显式带值，见红线 1） |

**`brain_author_defaults`（author 回落表，8 行）**

| machine | platform | author | 说明 |
|---|---|---|---|
| `server` | `*` | `uid:0` | 服务器本机 |
| `server` | `weixin` | `wx:o9cq801NZ88WjkV2_ETVKFkwkTEk` | 微信 openid |
| `r730` | `*` | `uid:0` | |
| `r9000p-Windows` | `*` | `win:local` | Windows 无 POSIX uid |
| `x260-debian` / `gigabyte-debian` | `*` | `uid:?` | 没采到实值，待校正 |
| `*` | `weixin` | `wx:?` | 全局微信兜底 |
| `*` | `*` | `unknown` | 最终兜底 |

列：`machine`+`platform` 复合主键、`author`、`note`。回落顺序：`(machine,platform)` → `(machine,'*')` → `('*',platform)` → `('*','*')`。
新机器接入必须往这里加一行，否则 author 落 `unknown`。

### 3.10 图谱层：`brain_entities` / `brain_edges` / `brain_entity_mentions`（v11）

第三路召回的离线结构。**只由 `graph-build`（人工）与维护线程的增量游标写入**，在线召回只读。

**`brain_entities`（实体，name 唯一）**

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT UNSIGNED PK | |
| `name` | VARCHAR(191) UNIQUE | 稳定短名；`User`/`用户`/`system` 等泛称在写入前就被过滤 |
| `kind` | VARCHAR(32) | server / project / service / person / device / topic |
| `mention_count` | INT UNSIGNED | 冗余计数，每次抽取后按 mentions 重算 |
| `machine` / `agent` | VARCHAR | 抽取发起方（归属惯例同其它表） |

**`brain_edges`（有向关系，src+dst+relation 唯一）**

| 列 | 说明 |
|---|---|
| `src_id` / `dst_id` | → `brain_entities.id`，`idx_edge_src` / `idx_edge_dst` |
| `relation` | 只允许 `runs_on` / `uses` / `part_of` / `related` |
| `weight` | DECIMAL(6,3)，同一条边重复抽取取 GREATEST |
| `evidence_memory_id` | 预留：抽取来源记忆（当前 LLM 不给，留空） |

**`brain_entity_mentions`（提及，entity_id + memory_id 复合主键）**

提及归属是**确定性**的：实体名是否出现在该记忆正文里（`name.lower() in content.lower()`），
不用模型给的 id —— 模型会编 id，但编不出「名字出现在正文里」。

```
2026-10-29 生产：264 实体 / 461 关系 / 1135 提及 / 覆盖 405 条活跃记忆（90.6%）
brain_meta.graph_cursor  = 增量游标（最大已处理记忆 id，首次只对齐、不回溯）
brain_meta.last_graph_build = 上次增量抽取时间（节流用，默认 10 分钟一次）
```

---

## 4. 视图（10 个，只读）

分三类，都只是 SELECT 的别名，不存数据：

**① 最新在前**（列表/浏览用，别直接扫裸表）

| 视图 | 基于 | 排序 |
|---|---|---|
| `brain_memories_newest` | `brain_memories` | `updated_at DESC` |
| `brain_turns_newest` | `brain_turns` | `ts DESC` |
| `brain_sessions_newest` | `brain_sessions` | `last_active DESC` |

外层自带 `ORDER BY` 时以外层为准；带 WHERE 过滤也保持“新在前”。

**② 北京时间**（所有 `datetime` 列 +8 小时，显示用，不要拿它做写入/对比）

`brain_memories_bj` · `brain_turns_bj` · `brain_turn_messages_bj` · `brain_sessions_bj` · `brain_archive_bj`
（库内一律 UTC，见第 2 节；只有给人看的时候才走 `_bj`，明细见 `brain_thoughts_detail_bj`。）

**③ 思考链宽表**（2026-10-25 加）

| 视图 | 基于 | 说明 |
|---|---|---|
| `brain_thoughts_detail` | `brain_thoughts LEFT JOIN brain_turns ON turn_id` | 一条思考 = 一行，把轮次上下文并进来 |
| `brain_thoughts_detail_bj` | 同上 | 同上，两个时间列换成北京时间 |

列：`turn_id, seq, thought_ts, turn_ts, session_id, platform, author, machine, message_count,
finish_reason, reasoning_chars, reasoning, user_chars, user_preview(200 字), assistant_chars, assistant_preview(200 字)`。

- `reasoning` 是全文；问答只给 200 字预览，全文去 `brain_turns` 按 `turn_id` 取（视图里复制全文会撑爆 `SELECT *`）。
- `LEFT JOIN` **故意保留孤儿**：父轮被删（修剪/去重/测试清理）后，思考行不会连锁删除。
  2026-10-25 晚实测 451 行属已删除的 turn 1107–1173（最多的 turn 1170 有 188 行），它们的
  `session_id / platform / turn_ts` 为 NULL —— 这是缺父行，不是索引没写。
  自查：`SELECT COUNT(*) FROM brain_thoughts t LEFT JOIN brain_turns r ON r.id=t.turn_id WHERE r.id IS NULL;`
  要清就按同一条件 `DELETE`（先确认不是别的机器正在回填）。
- `brain_thoughts.ts` 只在写入方带了消息级 `timestamp` 时才有值（实时轮次有，早期回填的没有），已按同一 `(turn_id, seq)` 的 `brain_turn_messages.ts` 补齐过；
  截至 2026-10-25 晚 7,784 行里仍有 275 行为空（多为孤儿行）；upsert 用 `COALESCE` 防回放擦除。
- 走 `uniq_thought(turn_id, seq)` 索引，按轮次查是 `ref` 而不是全表扫；跨轮按时间检索请用 `thought_ts` 上的 `idx_thought_ts`。

```sql
-- 某一轮的思考链（带提问）
SELECT seq, thought_ts, reasoning_chars, user_preview, LEFT(reasoning,200)
FROM brain_thoughts_detail WHERE turn_id = 1150 ORDER BY seq;

-- 最近一周某个平台的思考
SELECT turn_id, seq, thought_ts, LEFT(reasoning,120)
FROM brain_thoughts_detail_bj
WHERE platform = 'weixin' AND thought_ts >= NOW() - INTERVAL 7 DAY
ORDER BY thought_ts DESC LIMIT 20;
```

---

## 5. 触发器（10 个，库侧兜底，别关）

| 触发器 | 表 | 作用 |
|---|---|---|
| `trg_brain_turns_platform_bi/bu` | brain_turns | platform 小写化 + 别名归并（`cli`→`hermes-cli`、`desktop|local|tui|gui`→`hermes-desktop`、空→`unknown`） |
| `trg_brain_sessions_platform_bi/bu` | brain_sessions | 同上 |
| `trg_brain_archive_platform_bi/bu` | brain_archive | 同上 |
| `trg_brain_turns_author_bi/bu` | brain_turns | author 规范化：`*@im.wechat`→`wx:<oid>`、`cron/oneshot/subagent`→`sys:<platform>`、空→查 `brain_author_defaults` |
| `trg_brain_vector_index_embedded_bi/bu` | brain_vector_index | `embedded_at` 强制 `UTC_TIMESTAMP()`；`status<>'ok'` 置 NULL |

⚠️ 触发器绑在表上：`RENAME TABLE` 后会跟着**旧表**走。重建/重排表之后必须重跑
`/root/agent-db/apply-platform-triggers.py` 与 `apply-author-rules.py`，查 `information_schema.triggers` 应有 10 条。

---

## 6. Redis 短期层（db0=server，其它机器 db1..db4）

| 键 | 类型 | TTL | 内容 |
|---|---|---|---|
| `brain:recent` | Stream（MAXLEN≈2000） | 14 天 | 最近回合：sid / platform / author / machine / user / assistant（各截 600 字）/ ts |
| `brain:session:<sid>` | Hash | 14 天 | 会话状态：platform / last_active / turns |
| `brain:hot:<id>` | String(JSON) | 30 天 | 记忆热点镜像（写入时快照；`atbrains_recent(memories)` 直接读它） |
| `brain:hot:index` | ZSet | 30 天 | 热点镜像索引（forget 时 ZREM） |
| `brain:qcache:<hash>` | String | 10 分钟 | 检索结果缓存 |
| `brain:qemb:<hash>` | String | 1 小时 | 查询向量缓存 |

一机一库号：server=db0、x260-debian=db1、r9000p-Windows=db2、gigabyte-debian=db3、r730=db4。
**短期层永不跨机同步**；改 `machine` 后要跑 `/root/agent-db/refresh-hot-machine.py` 同步热点标签。

---

## 7. Qdrant

- 地址 `127.0.0.1:6333`，集合 **`brain_memories`**，维度 2560（`Qwen/Qwen3-Embedding-4B`）。
- 只索引 `brain_memories.content`；对话转录、思考、工具调用**不进向量**。
- key 只存在 `/root/qdrant/qdrant-config/production.yaml`（`service.api_key`），改 key = 改文件 + `docker restart qdrant`。
- 向量层故障只降级为纯关键词召回，不阻断读写。

---

## 8. 保留策略

| 数据 | 默认 | 说明 |
|---|---|---|
| `brain_turns` + `brain_turn_messages` + `brain_thoughts` + `brain_tool_calls` | **永久**（`turns_keep_days=0`） | 这是“记忆”本身，删了不可重建 |
| `brain_memories`（活跃）、claims/versions/evidence/episodes | **永久** | 同上 |
| `brain_sessions` | 365 天（`sessions_keep_days`） | 只是索引，可从 turns 聚合重建；且仅删无近期轮次的行 |
| `brain_archive` | 365 天 | 原件在 state.db 与 turns 里 |
| 已停用记忆（`active=0`） | 365 天（`inactive_keep_days`） | 兑现“忘掉”的语义 |

prune 由插件内维护线程执行（`GET_LOCK('brain_prune')`，每 7 天一次），也可 `python3 /root/agent-db/prune.sh`。

---

## 9. 迁移史

| 版本 | 内容 |
|---|---|
| v1 | `machine` 列（多设备归属） |
| v2 | 活跃行唯一键 `uniq_active_content`、召回索引、`deactivated_at`、CHECK、`truncated`、archive 来源列 |
| v3 | `brain_vector_index` 表 |
| v4 | 记忆分层（memory_type）+ 取代（superseded_by）+ 衰减 + rerank 开关 |
| v5 | 全量转录层 `brain_turn_messages` + `archived_messages` 游标 |
| v6 | 知识层：claims / versions / evidence / episodes |
| v7 | `agent` 列（memories / claims / episodes） |
| v8 | `brain_claim_versions.agent`（版本级作者） |
| v9 | `brain_thoughts` + `brain_tool_calls`（思考链与工具调用明细，2026-10-25） |
| v10 | 访问反馈层（`access_count` / `last_accessed_at`）+ 按类型 TTL（`expires_at`）+ `idx_access` / `idx_expires`（2026-10-29） |
| v11 | 图谱层：`brain_entities` / `brain_edges` / `brain_entity_mentions`（第三路召回，2026-10-29） |

---

## 10. 常用查询

```sql
-- 活跃记忆（最新在前）
SELECT id, kind, importance, machine, agent, LEFT(content, 80)
  FROM brain_memories_newest WHERE active = 1 LIMIT 20;

-- 某会话的轮次时间线
SELECT id, platform, author, message_count, LEFT(user_content, 40), ts
  FROM brain_turns WHERE session_id = ? ORDER BY ts;

-- 某轮的思考链 + 工具调用
SELECT seq, LEFT(reasoning, 120) FROM brain_thoughts WHERE turn_id = ? ORDER BY seq;
SELECT seq, tool_name, status, duration_ms, LEFT(args, 60)
  FROM brain_tool_calls WHERE turn_id = ? ORDER BY seq, call_index;

-- 最慢的终端命令
SELECT turn_id, seq, duration_ms, LEFT(args, 80)
  FROM brain_tool_calls WHERE tool_name = 'terminal'
 ORDER BY duration_ms DESC LIMIT 10;

-- 记忆写入方分布
SELECT agent, COUNT(*) total, SUM(active = 1) active FROM brain_memories GROUP BY agent;

-- 健康自检（都应为 0）
SELECT COUNT(*) FROM brain_turn_messages m LEFT JOIN brain_turns t ON t.id = m.turn_id WHERE t.id IS NULL;
SELECT COUNT(*) FROM brain_thoughts x LEFT JOIN brain_turns t ON t.id = x.turn_id WHERE t.id IS NULL;
SELECT COUNT(*) FROM brain_tool_calls x LEFT JOIN brain_turns t ON t.id = x.turn_id WHERE t.id IS NULL;
SELECT COUNT(*) FROM brain_thoughts WHERE reasoning_chars = 0;
SELECT COUNT(*) FROM brain_tool_calls WHERE call_id = '';
```

---

## 11. 改这张库之前

1. 先 `mariadb-dump agent_memory`（没有自动备份），`gzip -t` 验完整性再动手。
2. 新表/新列 = `SCHEMA_VERSION + 1` + 新迁移块，**不要修改已落库的 `_MIGRATIONS[n]`**。
3. 改完跑：`hermes atbrains-memory doctor`（版本/表结构）→ `status`（连通/计数）→
   `python3 /root/atbrains/scripts/contract-v1.py`（`/v1` 契约）→ `hermes atbrains-memory supersede-detect`（预演）。
4. 回填脚本一律幂等（`--dry-run` 先跑），并按 `turn_id` 分批，别一次性把 2 万行灌进去。
