---
name: brain-memory
description: "Use when saving/recalling durable facts or inspecting the brain store (Redis short-term + MariaDB long-term + Qdrant vector layer)."
version: 1.4.0
---

# brain 记忆库使用指南

brain 是本机自托管的双层记忆库，已作为 Hermes 记忆提供者激活：

- **Redis（短期）**：最近对话流 `brain:recent`、会话状态 `brain:session:*`、热点记忆 `brain:hot:*`、检索缓存。
  TTL 自动过期，不需要手工维护。用于「最近发生了什么」类问题和快速命中。
- **MariaDB（长期）**：`agent_memory` 库。`brain_memories`（事实/偏好/决策）、
  `brain_claims` + `brain_claim_versions` + `brain_claim_evidence`（结构化主张 / 版本 / 证据链）、
  `brain_episodes`（每轮情节头：目标/决策/未完成/下一步）、`brain_turns`（全部对话归档）、
  `brain_turn_messages`（全量转录层：工具调用/工具输出/推理，zlib 压缩）、`brain_sessions`（会话索引）、`brain_archive`（压缩前存档）。永久保存。
- **Qdrant（向量层）**：本机 `127.0.0.1:6333` 集合 `brain_memories`（2560 维，Qwen3-Embedding-4B / SiliconFlow）。
  写入后台异步向量化（无人值守兜底=进程内巡检（长命进程每 60 秒扫一次；systemd 定时器已退役））；检索为「关键词 + 向量」混合（RRF）；向量不可用时自动退回纯关键词。
  key 存放在 qdrant 的配置文件里（`/root/qdrant/qdrant-config/production.yaml` 的 `service.api_key`，只读挂载进容器；不在 Hermes 配置中，插件直接读取）。

## 何时用什么

- 用户说「记住/别忘了/以后都用…」→ `brain_remember`（kind=preference/fact，带 tags）。
- 需要「X 的 Y 是 Z」这类可核对、会变化的结论 → `brain_claim_remember(subject, predicate, value)`：
  同槽位再写只递增版本（`brain_claim_versions`），旧版本自动封版；可用 `turn_id`/`seq`/`quote` 挂证据。
  `brain_claim_recall` 取当前值，`history=true` 取全部版本。
- 一轮的目标/决策/未完成项/下一步 → `brain_episode_set(turn_id, …)`；`brain_turn_detail` 会带出该轮 episode。
- 需要找回旧信息（用户问「上次那个…」「之前说过…」）→ `brain_recall`；每轮对话也会自动预取注入。
- 想知道最近聊了什么 → `brain_recent`。
- 想复盘某一轮到底跑了哪些命令、返回了什么 → `brain_turn_detail`（turn_id 或 session_id，max_chars 控制每条上限）。
- 库的规模/健康检查 → `brain_stats`。
- 记错了 → `brain_forget`（软删除，需要 id）。

## 不写进 brain 的东西

- 敏感凭据（密码、token）不要用 brain_remember 存明文；改用 .env。
- 短命的临时状态（本轮任务进度）不必存 —— 对话归档自动覆盖。

## 维护（CLI）

```
hermes brain status / doctor      # 体检
hermes brain search <关键词>       # 人工检索（关键词+语义混合）
hermes brain prune --dry-run      # 清理预演（对话默认不清理；会话/存档/软删记忆按保留期 365 天）
hermes brain vector-status        # 向量层：开关/模型/key 来源/已向量化统计
hermes brain vector-drain         # 手动补向量（--force 全量重嵌）
hermes brain vector-test <查询>    # 同一查询：关键词/向量/融合 三方对比
```

配置：`~/.hermes/brain.json`（非密）+ `~/.hermes/.env`（BRAIN_REDIS_PASSWORD / BRAIN_MARIADB_PASSWORD）。

## 共享库约束（所有实例共同遵守，2026-10-24）

本库是全机共用的：`agent_memory`（MariaDB）。写入前先读这里的口径，别自己发明取值。

### platform 词表（brain_turns / brain_sessions / brain_archive）

`cli` / `desktop` / `weixin` / `telegram` / `discord` / `slack` / `email` / `cron` / `oneshot` / `subagent` / `unknown`（全小写）。
别名归并：`local|tui|gui → desktop`、`tool → cli`、空 → `unknown`；新平台小写透传，不加 CHECK/ENUM。
已由库侧触发器强制，写 `local`/`tool` 会被改写，不是 bug。

### author 词表（仅 brain_turns）

`uid:<n>`（Linux/macOS）/ `uid:?`（POSIX 但没采到 uid）/ `win:local`（Windows；Hermes 在 nt 上拿不到 POSIX uid，是平台特性）/
`wx:<openid>`（老的裸 `x@im.wechat` 统一成 wx:）/ `sys:cron|oneshot|subagent` / `unknown`。
每机默认值在 `brain_author_defaults(machine, platform, author)`，四级回落；新机器接入必须加一行
（Linux `uid:<id -u>`，Windows `win:local`），否则落 `unknown`。触发器 `trg_brain_turns_author_bi/bu` 强制。

### 改数据红线

1. 改 `brain_vector_index` 必须显式带 `updated_at`（否则 `ON UPDATE CURRENT_TIMESTAMP` 把全表顶成 now）。
2. 回填 `brain_turn_messages.ts` 用源消息时间（state.db `messages.timestamp`，UTC naive），不能用插入时间。
3. 重复轮判定键 `session_id+ts+user_content`，保留 `message_count` 最大者，连带删转录并重算 `turn_count`。
4. 删/回填后跑 `reorder-transcript.py` 恢复 1..N 与时间序；`RENAME` 会把触发器留给旧表，
   脚本已自动重建或在服务器跑 `apply-platform-triggers.py` + `apply-author-rules.py`。
5. 改表结构前先 mysqldump（没有自动备份）。
6. `embedded_at` 由触发器取 `UTC_TIMESTAMP()`，不要用进程本地时钟。

### agent / machine（跨 agent 共享，schema v8）

记忆只有一张 `brain_memories` 表，Hermes / omp / DeepSeek Harness(dsh) / artex 共写共读：

- `agent` = 写入方身份（Hermes 固定 `hermes`；MCP 通道 = 令牌名 `omp`/`dsh`/…）。
- `machine` = 来源主机（`server`/`r9000p-Windows`/…）；MCP 写入记服务端主机。
- 召回默认**跨 agent**（不过滤）；`brain_recall(agent="dsh")` 只看某一方。
- 结构化主张：`brain_claims.agent` 是当前值作者，`brain_claim_versions.agent` 是每一版作者。
- **不要再建 `<agent>_agent_memory` 分库**：分库会让记忆不通用。旧的三个分库已并入共享库。

### Qdrant / 实例边界

Qdrant（`http://111.230.71.79:6333`，集合 `brain_memories`）是**单一共享集合**，payload 带 machine/agent，
语义召回跨机（默认不按 machine/agent 过滤）。写向量只有服务器实例在做（drainer 扫全库），
别机只读；别机没有 Qdrant key 时向量层自动降级纯关键词。不要另建同名集合或改集合名。
omp/dsh/artex 通过 MCP 端点（8787 `/mcp`）读写同一个共享库，不单独建库。

完整版：服务器 `/root/agent-db/CONVENTIONS.md`（随插件包一起分发为 `CONVENTIONS.md`）。
