# 多 agent 接入 atbrains 长期记忆（MCP）

brain 插件本身是 **Hermes 专属**的 memory provider：只有 Hermes 会加载它、调用它的
prefetch/sync_turn 钩子。omp、DeepSeek Harness（dsh）、Codex、Claude Code 等 agent
不能直接加载这个插件。

从 2026-10-24 起，atbrains 的控制台进程额外暴露一个 **MCP 端点** `/mcp`，把同一套
brain 工具（记忆、检索、主张、情节）开放给任何支持 MCP 的 agent。

## 模型：一个共享库、一张表、agent 列

- **长期层只有一个 MariaDB 库**：`agent_memory`；记忆只有一张
  `brain_memories` 表。Hermes、omp、dsh 写入的行都在同一张表里，召回默认跨 agent ——
  你对 omp 说过的偏好，Hermes 也能 recall 到。
- **两条来源列，各管一件事**（schema v8 起）：
  - `agent`：写入方身份（`hermes` / `omp` / `dsh` / `artex` / `codex` …）。
    MCP 通道由令牌反查，客户端在请求体里伪造无效。
  - `machine`：来源主机（`server` / `r9000p-Windows` / `x260-debian` …）。
    MCP 客户端没有主机信息，写入点就是跑 MCP 端点的这台机器，故记服务端主机名。
- **旧的 `omp_agent_memory` / `dsh_agent_memory` / `artex_agent_memory` 分库已不存在**：
  分库会让记忆天然不通用（同一条事实三份副本、互相召回不到），现已全部并入共享库，
  用 `agent` 列区分来源；需要严格隔离时也不建分库，改用 `agent` 过滤。
- **召回默认不过滤 agent**；只看某一方就传 `agent` 参数（MCP 与 Hermes 的
  `brain_recall` 都支持）。结构化主张（claims）的版本也记录了写入方 agent。
- **Redis 只是热点缓存**：召回缓存、最近记忆索引，丢了会自动重建；权威数据在 MariaDB。

这与 scope-recall 的 "shared store, each memory says which agent it came in through"
是同一模型，但底层换成 atbrains 已有的 Redis + MariaDB + Qdrant 管线。

## 端点与鉴权

```
POST http://<服务器>:8787/mcp
Authorization: Bearer <service-tokens.json 里该 agent 的令牌>
Content-Type: application/json
```

- 单条 JSON-RPC 2.0 或批量数组都支持；响应是 `application/json`（不是 SSE 流）。
- `GET /mcp` 返回 405（无服务端推送流），`DELETE /mcp` 返回 200（无状态）。
- 令牌表：`~/.hermes/plugins/atbrains/service-tokens.json`（600，运行时状态，**不进 git**）。
  用 `hermes atbrains token-add <agent>` 生成/轮换（明文只显示一次），
  `token-list` 只看掩码，`token-rm` 吊销。
- 控制台的网页口令（`console/.token`）和 MCP 的服务令牌是两套，互不通用。

## 工具

MCP 工具名与 Hermes 侧一致，客户端通常会加前缀（omp/dsh 显示为
`mcp__brain__brain_remember`）：

| 工具 | 用途 |
|---|---|
| `brain_remember` | 保存一条长期记忆（fact/preference/decision/note/todo） |
| `brain_recall` | 混合召回（关键词 + 向量 RRF），附历史对话与结构化主张 |
| `brain_forget` | 按 id 软删除一条记忆 |
| `brain_recent` | 最近写入的记忆 / 最近对话流 |
| `brain_stats` | 本 agent 身份、库名、计数、存储占用 |
| `brain_turn_detail` | 某一轮的完整工具过程（转录层） |
| `brain_claim_remember` / `brain_claim_recall` | 结构化主张（同槽位版本化） |
| `brain_episode_set` / `brain_episode_get` | 某一轮的目标/决策/未完成项 |

`initialize` 会返回一段 `instructions`，告诉模型什么时候该 recall、什么时候该 remember。
不同 agent 拿到的 instructions 会带上自己的 agent 名。

## omp（Oh My Pi）

用户级配置：`~/.omp/agent/mcp.json`（600）：

```json
{
  "mcpServers": {
    "brain": {
      "type": "http",
      "url": "http://127.0.0.1:8787/mcp",
      "headers": {"Authorization": "Bearer <omp 的令牌>"},
      "enabled": true
    }
  }
}
```

验证：启动 omp 后工具列表里应出现 `mcp__brain__brain_*`；让它调用 `brain_stats`，
应返回 `agent: omp`、`database: agent_memory`。

> 本机验证脚本（SDK 方式，不依赖交互式 CLI）：
> `cd ~/.bun/install/global/node_modules/@oh-my-pi/pi-coding-agent && bun examples/sdk/99-brain-mcp-test.ts`

## DeepSeek Harness（dsh）

dsh 用 Cordis 插件行接入 MCP。把下面一行追加到 `$DSH_HOME/cordis.patch.yml`
（默认 `~/.dsh/cordis.patch.yml`；已有内容时合并列表，不要覆盖）：

```yaml
- id: mcp-brain
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: brain
    transport: streamable-http
    url: http://111.230.71.79:8787/mcp
    headers:
      Authorization: !!js '`Bearer ${process.env.ATBRAINS_MCP_TOKEN}`'
```

设置环境变量（Windows PowerShell：`setx ATBRAINS_MCP_TOKEN "<令牌>"`；bash：
`export ATBRAINS_MCP_TOKEN="<令牌>"`），重启 `dsh web`，新会话里工具名应为
`mcp__brain__brain_*`。

如果 8787 从你的网络不可达，先建 SSH 隧道再把 url 换成 `http://127.0.0.1:8787/mcp`：

```bash
ssh -p 7622 -L 8787:127.0.0.1:8787 root@111.230.71.79
```

## 验收（不依赖任何 agent）

```bash
python3 /root/agent-db/mcp_client_test.py --url http://127.0.0.1:8787/mcp --agent omp --write-test
```

应输出 `initialize` / `tools: 10` / `stats: agent=omp db=agent_memory` /
`remember: True` / `recall: ... target_found= True` / `forget: True` /
`bad token status: 401`。

## 边界与注意

- **不会自动归档非 Hermes agent 的对话**：`brain_turns` 的写入依赖 Hermes 的
  `sync_turn` 钩子。omp/dsh 只有显式调用 `brain_remember` 的内容会进库。
  需要「每轮全量转录」的话，得给对应 agent 写 hook/plugin，不是 MCP 能解决的。
- **`brain_forget` 是全局的**：共享库里任何 agent 都能停用任何 id 的记忆（与 Hermes 同等权限）。
- **向量召回在服务端做**：Qdrant/embedding 都在服务器上，客户端不需要装向量依赖；
  向量层故障时自动回落关键词。
- **刚写入的低重要度记忆**：MCP 的 `brain_recall` 会先做一次「整句精确包含」直查，
  避免被高重要度旧条目挤出 top-k；Hermes 侧排序未改。
- 令牌泄露 = 记忆库读写权限泄露。不要把令牌写进 git、聊天记录或截图；
  泄露后 `hermes atbrains token-add <agent>` 轮换即可。
