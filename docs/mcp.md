# atbrains MCP

atbrains 把「共享长期记忆」按 [Model Context Protocol](https://modelcontextprotocol.io) 暴露给任意
MCP 客户端（Claude Code、Codex、Cursor、Claude Desktop、omp、其它 harness）。
一份工具声明、两种传输、同一个 MariaDB/Redis/Qdrant 库。

参考实现：[Riley-Coyote/mnemos-mcp](https://github.com/Riley-Coyote/mnemos-mcp)。
我们借的是它的**形状**，不是它的数据模型：

| 借来的 | 没借的（atbrains 有意不做） |
|---|---|
| 小而顺手的工具面（会话开头一次 `context`，收尾一次 `handoff`） | 以「一个文件」为中心的本地单机存储 —— atbrains 是跨机共享库 |
| `xxx connect <client> --write` 一键写客户端配置 | 衰减/遗忘曲线、梦境、日记、beliefs 元层 —— 与「可核对的长期事实」目标不同 |
| 服务器级 `instructions` 教模型怎么用记忆 | 每会话独立 agent 记忆 —— atbrains 是多 agent 共库、`agent` 列标来源 |
| 工具 `annotations`（只读/破坏性/幂等） | 外部嵌入依赖 —— 向量走本机 Qdrant 服务 |

## 传输

### 1. stdio（推荐给本机客户端）

```bash
python3 <插件目录>/mcp_stdio.py --agent codex
```

- 客户端把它当子进程拉起，**不经过网络、不需要令牌**。
- 协议：一行一条 JSON-RPC 2.0（单条或批量），stdout 只走协议，日志走 stderr。
- 身份：`--agent`（缺省 `$ATBRAINS_AGENT`，再缺省 `stdio`），写进记忆的 `agent` 列。
- 需要能找到 `atbrains-memory` 插件的 `core.py`：`$ATBRAINS_MEMORY_DIR` →
  `$HERMES_HOME/plugins/atbrains-memory` → 仓库同级目录。

### 2. streamable HTTP（推荐给跨机/常驻）

```
POST http://<服务器>:8787/mcp
Authorization: Bearer <service-tokens.json 里的令牌>
Accept: application/json, text/event-stream
```

- 与 `/v1`、网页控制台共用一个端口（默认 8787）。
- 令牌名就是 agent 身份；令牌名可写 `<主机>|<agent>`（MCP 只取 agent 侧）。
- 无状态：客户端带不带 `Mcp-Session-Id` 都接受；`DELETE /mcp` 返回 200。

## 协议版本

支持 `2025-06-18`（默认）、`2025-03-26`、`2024-11-05`：客户端报什么就回什么，
报了个不认识的版本则回最新版。`initialize` 返回 `capabilities`：
`tools` / `resources` / `prompts` / `logging`。

## 工具

| 工具 | 入参要点 | 返回 |
|---|---|---|
| `atbrains_context` | `focus?`, `limit?`(8), `include_turns?`, `include_episodes?`, `max_chars?`(400) | `{ok, agent, focus, memories[], claims[], open_episodes[], recent_turns[], counts{}}` |
| `atbrains_recall` | `query`, `limit?`, `kind?`, `agent?`, `include_turns?` | `{ok, memories[], turns[], claims[], agent}` |
| `atbrains_remember` | `content`, `kind?`, `tags?`, `importance?` | `{ok, saved, storage}` |
| `atbrains_forget` | `id` | `{ok, result}` |
| `atbrains_handoff` | `note`, `next_step?`, `open_items?`, `turn_id?`, `importance?`(7) | `{ok, saved, hint, episode?}` |
| `atbrains_claim_remember` | `subject`, `predicate`, `value`, `kind?`, `basis?`, `evidence?`, `confidence?` | `{ok, claim}` |
| `atbrains_claim_recall` | `query?`/`subject?`/`predicate?`, `history?` | `{ok, claims[], count}` |
| `atbrains_episode_set` | `turn_id`, `goal?`, `decisions?`, `open_items?`, `blockers?`, `next_step?`, `status?` | `{ok, episode}` |
| `atbrains_episode_get` | `turn_id?` / `session_id?` | `{ok, ...episode}` |
| `atbrains_recent` | `n?`(10), `what?`(`memories`/`turns`) | `{ok, recent_memories[]/recent_turns[]}` |
| `atbrains_turn_detail` | `turn_id?` / `session_id?`, `max_chars?` | `{ok, found, turn, messages[], episode}` |
| `atbrains_stats` | — | `{ok, stats{}}` |

约定：

- `brain_*` 是旧名别名（`TOOL_ALIASES`），老客户端不用改。
- 工具异常回 `{ok:false,error}` 且 `isError:true` —— 模型能看到原因，传输层不崩。
- 每个工具带 `annotations`：`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`。

### 醒来包为什么单独做一个工具

`atbrains_recall` 回答「和这个问题有关的记忆是什么」；`atbrains_context` 回答
「我现在是谁、记着什么、上次做到哪」。会话开头只要一次调用就能拿到：

1. 记忆（带 `focus` 走向量+关键词召回，不带则取高重要度）
2. 结构化主张（subject/predicate/value，带版本）
3. 未完成情节（`brain_episodes status=open`）
4. 最近对话一行摘要
5. 计数（记忆/轮次/主张/情节/会话）

正文按 `max_chars`（默认 400）截断并标 `truncated` —— 醒来包是索引，不是全文库。

### 交接便签

`atbrains_handoff` 写一条 `kind=note, tags=handoff` 的记忆（默认 `importance=7`），
可选把 `next_step`/`open_items` 挂到某一轮的情节头上。下一次会话的
`atbrains_context` 会在最近记忆与未完成情节里看到它。

## 资源

| URI | mimeType | 内容 |
|---|---|---|
| `atbrains://context` | `text/markdown` | 醒来包 |
| `atbrains://stats` | `application/json` | 库统计 |
| `atbrains://recent/memories` | `application/json` | 最近 20 条记忆 |
| `atbrains://recent/turns` | `application/json` | 最近 20 轮对话摘要 |

## 提示词

| 名称 | 参数 | 作用 |
|---|---|---|
| `wake-up` | `focus?` | 把醒来包作为会话的第一条上下文注入 |
| `handoff` | `next_step?` | 引导模型写交接便签并调用 `atbrains_handoff` |

## 安装

```bash
hermes atbrains mcp doctor                      # 自检：12 工具 / 4 资源 / 2 提示词 + 真实召回
hermes atbrains mcp install claude-code --write  # ~/.claude.json
hermes atbrains mcp install cursor --write       # ~/.cursor/mcp.json
hermes atbrains mcp install codex --write        # ~/.codex/config.toml
hermes atbrains mcp install claude-desktop --write
hermes atbrains mcp print generic                # 任意客户端
```

- 默认只预演；`--write` 才落盘，JSON 配置先备份为 `*.bak-atbrains`。
- TOML（codex）只在缺少 `[mcp_servers.atbrains]` 时追加，不覆盖用户手写的 env/timeout。
- 写进配置的解释器优先 `/usr/bin/python3`（`$ATBRAINS_MCP_PYTHON` 可覆盖），
  避免绑定 Hermes 的工具缓存 Python（会被清理）。
- HTTP：`--transport http --agent omp --url http://host:8787/mcp`，令牌按 agent 名自动取。

## 安全

- **令牌只在服务端解析**：`/mcp` 的 Bearer 令牌决定 agent 身份，请求体里自报的 `agent` 不采信。
- **stdio 不落令牌**：身份由客户端配置里的 `--agent` 决定，只对本机进程生效。
- **`atbrains_forget` 是软删除**（`active=0`），可用 `hermes atbrains restore <id>` 恢复。
- 服务器级 `instructions` 会随 `initialize` 下发，明确告诉模型「检索为空就如实说没有记录，
  不要编造」。

## 已知边界

- stdio 模式下每建立一个 `agent` 就持有一个 `AtbrainsStore`（连接池复用），不做跨进程缓存。
- `atbrains_context` 依赖 `brain_episodes` 表（schema v8+）；老库没有该表时自动跳过情节段。
- 资源是快照，不实现 `resources/subscribe` 推送。
