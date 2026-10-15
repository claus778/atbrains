# brain — Redis（短期）+ MariaDB（长期）记忆插件

Hermes 记忆提供者插件：把两台本机服务接进 agent 大脑。

## 分层设计

| 层 | 服务 | 内容 | 生命周期 |
|---|---|---|---|
| 短期 | Redis `127.0.0.1:9079` | `brain:recent` 最近对话流（Stream）、`brain:session:*` 会话状态、`brain:hot:*` 热点记忆镜像、`brain:qcache:*` 检索缓存 | TTL 自动过期（默认 14-30 天），无需维护 |
| 长期 | MariaDB `127.0.0.1:57306` / 库 `hermes_agent_memory` | `brain_memories` 事实/偏好/决策、`brain_turns` 完整对话归档、`brain_sessions` 会话索引、`brain_archive` 压缩前存档 | 永久，`hermes brain prune` 按保留期清理 |

## 接入点（Hermes 钩子）

- `prefetch` / `queue_prefetch` — 每轮对话前从长期库召回相关记忆 + 历史对话（结果缓存进 Redis，10 分钟内同样的查询直接命中）。
- `sync_turn` — 每轮对话完成：写入 Redis 对话流 + MariaDB 归档 + 会话计数。
- `on_memory_write` — 内置 MEMORY.md / USER.md 的每次写入同步镜像到双存储。
- `on_pre_compress` — 上下文压缩前，将即将被丢弃的消息先存档进 `brain_archive`。
- `on_session_end` / `on_session_switch` — 会话边界维护。
- `on_delegation` — 子代理任务结果入库（kind=delegation）。

## 工具

`brain_remember` · `brain_recall` · `brain_forget` · `brain_recent` · `brain_stats`

## CLI

```
hermes brain status            # 连通性 / 配置 / 统计总览
hermes brain search <关键词>   # 检索记忆与历史对话
hermes brain remember <内容>   # 手工存一条长期记忆
hermes brain recent            # Redis 最近对话流
hermes brain forget <id>       # 废止记忆（软删除）
hermes brain prune --dry-run   # 清理预演
hermes brain doctor            # 自检
```

## 配置

- 非密配置：`$HERMES_HOME/brain.json`（host/port/user/db + 行为参数，见 `core.py` DEFAULT_CONFIG）。
- 密码：`$HERMES_HOME/.env` 中 `BRAIN_REDIS_PASSWORD` / `BRAIN_MARIADB_PASSWORD`（经 `save_env_value` 写入）。
- 激活：`memory.provider: brain`（`hermes config set memory.provider brain`）。
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

## 设计说明

- 多设备共享：`brain_memories` / `brain_turns` / `brain_sessions` 均带 `machine` 列（来源机标识，取自 `brain.json` 的 `machine`，缺省=主机名）；召回片段自动标注来源。
- 检索使用 LIKE 词法匹配（中文按双字组切分），个人库规模下足够快；不依赖分词/嵌入服务。
- 写入路径失败只告警不抛出 —— 数据库暂不可达不阻塞对话。
- 所有时间戳为 MariaDB 服务器本地时间。
