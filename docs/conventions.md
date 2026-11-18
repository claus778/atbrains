> 表/列字典与 Redis/触发器细节见 `/root/atbrains/docs/database-schema.md`；`/v1` 契约见 `/root/atbrains/docs/protocol-v1.md`。

# brain 共享库约束（2026-10-24 定稿，所有 Hermes 实例共同遵守）

适用：所有写入 `agent_memory`（MariaDB 127.0.0.1:57306 / 111.230.71.79:57306）的 Hermes 实例。
库内时间一律 UTC（比北京时间慢 8 小时）。

## 0. agent / machine 口径（schema v8）

- **一张表**：hermes / omp / dsh / artex / codex 共用 `brain_memories`，不分库、不分表。
- `agent`：写入方身份，小写、≤32。Hermes 各实例固定 `hermes`；MCP 通道 = 令牌名。
- `machine`：来源主机（`server` / `r9000p-Windows` / `x260-debian` …）。MCP 客户端
  没有主机信息，写入点就是跑 MCP 端口的那台机器，记服务端主机名；**不要拿 agent 名冒充主机**。
- 列默认值 `'hermes'`：未升级的老进程 INSERT 不写 agent 时归属仍然正确。
- 召回默认跨 agent（不过滤）；`brain_recall(agent=...)` 只看某一方。
- `brain_claims.agent` = 当前值是谁写的；`brain_claim_versions.agent` = 每一版是谁写的。
- **禁止再建 `<agent>_agent_memory` 分库**：分库 = 记忆不通用（同一条事实多份副本、
  互相召回不到），已并入共享库；要隔离就用 `agent` 过滤。

## 0.1 兼容库 hermes_agent_memory（已删，2026-10-25）

改名时留下的兼容桥（旧库名 → 15 个指向 `agent_memory` 的可更新视图、0 张基表）已整体删除：
`DROP DATABASE hermes_agent_memory` + 收回 `agent@%` 对该库的授权；`mysql.db` 里重复的
`agent\_memory` 授权行也清了（只剩 `agent_memory`）。

- DDL 备份：`/root/agent-db/backup-hermes-compat-views-20261025-0232.sql`（10KB，含全部 15 个视图）。
- 若某台老客户端的 `~/.hermes/brain.json` 里 `mariadb_db` 还写着旧库名，会报
  `Unknown database 'hermes_agent_memory'`；恢复兼容桥只要把该备份用 stdin 喂给 mariadb
  （重建时 DEFINER 已去掉）。
- 现有库名只有一个：`agent_memory`。再看到旧名一律是过期文档/历史备份，不是活动对象。

## 1. platform 词表（brain_turns / brain_sessions / brain_archive）

规范值，全小写：
`hermes-cli` / `hermes-desktop` / `weixin` / `telegram` / `discord` / `slack` / `email` / `cron` / `oneshot` / `subagent` / `unknown`
（`hermes-*` = Hermes 自己的本机入口；别的 agent 用自家名字小写透传，例如 dsh 写 `dsh`）

别名归并（写入期，插件 `core.normalize_platform` 与库侧触发器各做一次）：
- `cli` / `tool` → `hermes-cli`（工具触发的会话按命令行入口算）
- `desktop` / `local` / `tui` / `gui` → `hermes-desktop`（本机控制套接字的桌面/TUI）
- 空值 → `unknown`
- 新平台（feishu/telegram…）小写透传，**不要加 CHECK/ENUM**，否则新平台写入直接失败

## 2. author 词表（只 brain_turns 有）

- `uid:<n>`      Linux/macOS 本机用户（POSIX uid）
- `uid:?`         POSIX 机器但从没采集到 uid
- `win:local`    Windows 本机用户（Hermes 的 `_posix_peer_subject` 在 nt 上返回 None，没有 POSIX uid，这是平台特性）
- `wx:<openid>`  微信（老的裸 `xxx@im.wechat` 统一成 wx: 前缀）
- `sys:cron` / `sys:oneshot` / `sys:subagent`  自动任务，无人类作者
- `unknown`      兜底

每台机器的默认值在表 `brain_author_defaults(machine, platform, author)`：
`(machine,platform)` → `(machine,'*')` → `('*',platform)` → `('*','*')` 四级回落。
新机器接入必须往这张表加一行（Linux 写 `uid:<id -u>`，Windows 写 `win:local`）。

## 3. 库侧触发器（已生效，别去关掉）

- `trg_brain_turns_platform_bi/bu`、`trg_brain_sessions_platform_bi/bu`、`trg_brain_archive_platform_bi/bu`
- `trg_brain_turns_author_bi/bu`
- `trg_brain_vector_index_embedded_bi/bu`（强制 embedded_at = UTC_TIMESTAMP()，status<>'ok' 置 NULL）

含义：**不管哪个客户端、哪版插件，写进来的值都会被规范化**。所以：
- 不要手工往这些表写 `local` / `tool` / 空 author，会直接被改写；
- 排查“为什么我写进去的值变了”，先看这几条触发器；
- `RENAME TABLE` 会把触发器留给旧表 —— 任何重建/重排表之后必须重跑
  `/root/agent-db/apply-platform-triggers.py` 与 `/root/agent-db/apply-author-rules.py`。
  校验：`information_schema.triggers` 里应有 8 条（platform 6 + author 2）+ vector_index 2 条。

## 4. 改数据的硬约束

1. **改 `brain_vector_index` 必须显式写 `updated_at = updated_at`**（或目标值），
   否则 `ON UPDATE CURRENT_TIMESTAMP` 会把全表 updated_at 顶成 now。
2. **回填转录 `brain_turn_messages.ts` 必须用源消息时间**（state.db messages.timestamp，UTC naive），
   不能用列默认值/插入时间，否则整表看起来“只导入了导入那一天”。
3. 重复轮次判定键：`session_id + ts + user_content`；保留 `message_count` 最大者，连带删转录、重算 `brain_sessions.turn_count`。
4. 删除/回填后不重编号；需要 1..N 连续与时间序时跑 `/root/agent-db/reorder-transcript.py --apply`（切换后自动重建触发器）。
5. 改表结构前先 `mysqldump`（无自动备份，别假设有）。

6. **`brain_sessions.archived_messages` 是绝对条数游标**：客户端上传 `messages[]` 时若只传
   尾部窗口（dsh 插件按 `archiveMaxMessages` 截断），必须同时传 `total`=会话消息总数，
   服务端用 `offset = total - len(messages)` 换算。不带 `total` 又截断窗口，会让增量恒为空
   （会话超过窗口长度后每轮 `message_count=0`）。排障：`message_count=0` 且 `archived_messages`
   正好等于窗口长度 = 这个坑，不是数据坏了。

## 5. Qdrant（http://111.230.71.79:6333，集合 brain_memories）

- **一个共享集合**，payload 带 `machine` 与 `agent`；所有机器的记忆都在里面，语义召回是跨机的
  （默认不按 machine/agent 过滤；按 agent 过滤在 SQL 侧完成）。
- **写向量只有服务器实例在做**：drainer 扫全库未向量化的记忆，不分 machine；别机只读（语义检索）。
- 别机没有 Qdrant key（key 只在服务器 `/root/qdrant/qdrant-config/production.yaml`），拉不到 key 时向量层自动降级为纯关键词，不影响关键词召回。
- 它不是“每台一份”，不要试图在别机建同名集合或改 collection 名。

## 6. 分发版本

`brain-plugin-1.2.2`（`/root/agent-db/dist/brain-plugin-1.2.2.tar.gz|.zip`）已含 `normalize_platform`；
更早的包没有。装包后仍受库侧触发器兜底，所以升级不紧急。

## 7. 一份可执行的自检（在任意可连库的机器上跑）

```sql
-- 都应为 0
-- 都应为 0（残留旧别名 / 空值 / 非小写；dsh 之类新平台不算错）
SELECT COUNT(*) FROM brain_turns WHERE platform='' OR platform <> LOWER(platform)
 OR platform IN ('cli','desktop','local','tui','gui','tool');
SELECT COUNT(*) FROM brain_turns WHERE author='' OR (author NOT LIKE 'uid:%' AND author NOT LIKE 'win:%'
 AND author NOT LIKE 'wx:%' AND author NOT LIKE 'sys:%' AND author<>'unknown');
SELECT COUNT(*) FROM (SELECT session_id,ts,user_content FROM brain_turns GROUP BY 1,2,3 HAVING COUNT(*)>1) x;
SELECT COUNT(*) FROM brain_turn_messages m LEFT JOIN brain_turns t ON t.id=m.turn_id WHERE t.id IS NULL;
SELECT COUNT(*) FROM brain_turns t WHERE t.message_count <> (SELECT COUNT(*) FROM brain_turn_messages m WHERE m.turn_id=t.id);
```

## 8. 思考链与工具调用明细（schema v9，2026-10-25）

原文仍在 `brain_turn_messages`（zlib(JSON) blob），下面两张是它的**投影**，用来按工具/状态/耗时检索：

- `brain_thoughts(turn_id, seq, message_uid, reasoning, reasoning_chars, finish_reason, ts)` —— 一条 assistant 消息一行
- `brain_tool_calls(turn_id, seq, call_index, call_id, tool_name, args, result, result_chars, status, started_at, ended_at, duration_ms)` —— 一次调用一行

口径：
- 只做索引，不参与召回、不进向量；`atbrains_turn_detail` 仍从原文取（更全，带工具输出原文）。
- `status` ∈ `ok` / `error` / `pending`（pending = 调用发出但没等到结果，通常是被中断的回合）。
- 幂等键 `(turn_id, seq)` / `(turn_id, seq, call_index)`，回填可反复跑。
- 回填：`python3 /root/agent-db/backfill-thoughts-toolcalls.py --apply`（`--only-missing` 只补缺的）。
- ⚠️ `TRUNCATE` 需要 DROP 权限，agent 账号没有 —— 清表用 `DELETE FROM ...`。
- ⚠️ tool 消息的 call id **只在 `brain_turn_messages.tool_call_id` 列里**，blob 里没有；回填不读这列会退化成
  “每条结果一行、call_id 为空”的幽灵行。
- ⚠️ `tool_calls` 在历史行里**可能是 JSON 字符串**，直接 `len()/enumerate()` 会逐字符迭代（一次能写出几百万行）。

常用查询：

```sql
-- 某个工具最慢的 10 次
SELECT turn_id, seq, duration_ms, LEFT(args,60) FROM brain_tool_calls
 WHERE tool_name='terminal' ORDER BY duration_ms DESC LIMIT 10;
-- 某轮的思考链
SELECT seq, LEFT(reasoning,200) FROM brain_thoughts WHERE turn_id=1234 ORDER BY seq;
-- 失败的工具调用
SELECT turn_id, seq, tool_name, LEFT(result,120) FROM brain_tool_calls
 WHERE status='error' ORDER BY id DESC LIMIT 20;
```
