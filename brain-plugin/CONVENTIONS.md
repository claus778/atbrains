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

## 1. platform 词表（brain_turns / brain_sessions / brain_archive）

规范值，全小写：
`cli` / `desktop` / `weixin` / `telegram` / `discord` / `slack` / `email` / `cron` / `oneshot` / `subagent` / `unknown`

别名归并（写入期，插件 `core.normalize_platform` 与库侧触发器各做一次）：
- `local` / `tui` / `gui` → `desktop`（本机控制套接字的桌面/TUI；Windows 桌面端本来就写 desktop）
- `tool` → `cli`（对齐 Hermes session_policy）
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
SELECT COUNT(*) FROM brain_turns WHERE platform NOT IN
 ('cli','desktop','weixin','telegram','discord','slack','email','cron','oneshot','subagent','unknown');
SELECT COUNT(*) FROM brain_turns WHERE author='' OR (author NOT LIKE 'uid:%' AND author NOT LIKE 'win:%'
 AND author NOT LIKE 'wx:%' AND author NOT LIKE 'sys:%' AND author<>'unknown');
SELECT COUNT(*) FROM (SELECT session_id,ts,user_content FROM brain_turns GROUP BY 1,2,3 HAVING COUNT(*)>1) x;
SELECT COUNT(*) FROM brain_turn_messages m LEFT JOIN brain_turns t ON t.id=m.turn_id WHERE t.id IS NULL;
SELECT COUNT(*) FROM brain_turns t WHERE t.message_count <> (SELECT COUNT(*) FROM brain_turn_messages m WHERE m.turn_id=t.id);
```
