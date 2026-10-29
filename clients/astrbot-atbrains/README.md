# astrbot-atbrains — AstrBot 的 atbrains 长期记忆插件

把 AstrBot（QQ 机器人框架）接入 atbrains 记忆库：每轮对话自动召回相关记忆、
提供 `memory_save` / `memory_search` 函数工具、每轮问答自动归档到
`brain_turns` / `brain_sessions` 与 Redis `brain:recent`。写入方身份固定为
`agent = 'astrbot'`，与 Hermes、dsh 等 agent 共享同一个 MariaDB 库。

写入统一走 `/v1`（失败降级本地直连）：machine 身份、`memory_type`/TTL、去重合并、
转录层由服务端决定；开关 `remote_write_enabled`（默认 1）。

## 能力

| 钩子 | 行为 |
|---|---|
| `filter.on_llm_request()` | 按本轮用户消息召回：`remote_recall_enabled=1` 时先 POST `/v1/memory/recall`（语义 + 关键词 RRF），失败降级本地关键词检索；拼到 `req.system_prompt`。用 `event.set_extra` 防止同一轮重复注入 |
| `filter.on_agent_done()` | 归档本轮问答：`remote_write_enabled=1` 时先 POST `/v1/memory/turn`，失败降级本地 `brain_turns` / `brain_sessions` + Redis `brain:recent` |
| `filter.llm_tool` | `memory_save` 写入统一走 `/v1/memory/remember`（失败降级本地直连）；`memory_search` 优先 `/v1/memory/recall`，空结果降级本地检索；docstring 里的 `Args:` 必须带 `(string)` / `(number)` 类型标注，否则 AstrBot 解析不出参数 schema |

插件对记忆库只做「读记忆 / 写记忆 / 写回合」三类操作：**不写转录层、不做向量化**
（向量回填由 Hermes 侧的 drainer 负责）。

## 目录

```
main.py         插件主体（约 880 行，单文件）
metadata.yaml   AstrBot 插件元信息
vendor/         pymysql + redis 源码（不走 pip，绕开国内装包）
```

`vendor/` 是源码内置依赖：`main.py` 启动时把 `vendor/` 插到 `sys.path` 前面，
因此不需要在 `/opt/astrbot/venv` 里额外 `pip install`。

## 安装

```bash
# 方式 A：用仓库出包脚本（zip 顶层目录就是插件 id `atbrains-memory`，解包即装好）
python3 tools/package-astrbot-client.py                # → dist/astrbot-atbrains-<版本>.zip
unzip -o dist/astrbot-atbrains-1.0.0.zip -d /opt/astrbot/data/plugins/

# 方式 B：直接拷源码
cp -r clients/astrbot-atbrains /opt/astrbot/data/plugins/atbrains-memory
chown -R root:root /opt/astrbot/data/plugins/atbrains-memory
systemctl restart astrbot          # 或用 WebUI 的插件热重载
```

## 配置

复用 Hermes 侧已有的配置文件，不额外维护一份：

- `/root/.hermes/atbrains.json` — host / port / db / machine 等非密配置
- `/root/.hermes/.env` — `ATBRAINS_MARIADB_PASSWORD`、`ATBRAINS_REDIS_PASSWORD`
  （兼容旧名 `BRAIN_MARIADB_PASSWORD` / `BRAIN_REDIS_PASSWORD`）
- `ATBRAINS_HERMES_HOME` 环境变量可覆盖配置目录（默认 `/root/.hermes`）

**载入顺序有讲究**：旧 `brain.json` 必须先于 `atbrains.json` 读入，后者同名键覆盖前者。
写反了会连到旧库里留的库名（如 `hermes_agent_memory`）。

### 性能开关（跨公网直连，默认全开）

AstrBot 常与记忆库**不在同一台机器**上（MariaDB/Redis 跨公网，RTT 40~65 ms 量级），
每轮成本几乎就是「往返次数 × RTT」。下面这组开关都写在 `~/.hermes/atbrains.json`，
**每个都能单独关掉，关掉即逐字节回到旧行为**（SQL 文本做过 A/B 比对）：

| 配置 | 默认 | 关掉（`=0`）时回到的行为 |
|---|---|---|
| `mysql_ping_interval` | `30` | 每条语句前都 `ping()`；窗口内直接复用连接（窗口外第一条语句多 1 次往返） |
| `recall_union_enabled` | `1` | 轮首召回拆成「记忆」「对话」两条 SELECT |
| `archive_multi_stmt` | `1` | 轮尾两条 INSERT 各占一次往返 |
| `recall_cache_enabled` | `1` | 召回不做进程内缓存，每次查库 |
| `recall_cache_ttl` | `300` | 缓存 TTL（秒）；`0` 视同关闭 |
| `recall_cache_max` | `256` | 缓存条数上限（LRU 淘汰） |
| `recall_timeout_ms` | `900` | 轮首召回不设超时（公网抖动会直接拖慢首 token） |
| `archive_async` | `1` | 归档在 `on_agent_done` 里同步 await |
| `search_regexp_enabled` | `1` | 关键词检索回到 terms×columns 个 OR-LIKE |

实测（45 ms 模拟 RTT 的往返计数，见 `atbrains` 仓库的 bench）：

- 轮首召回：**4 次往返 → 1 次**（约 185 ms → 48 ms）；同一句话重复问命中缓存 = **0 次往返**。
- 轮尾归档：**5 次往返 → 2 次**（MariaDB 1 + Redis 1，约 232 ms → 102 ms），
  且在后台任务里跑，不挡回合收尾。
- 最坏情况（全不命中的关键词检索）不再随词数线性变慢：1/5/10 词
  319.5 ms → 8.5 ms（单次 REGEXP 扫描）。

缓存键含召回条数与时间窗维度，改配置不会读到旧口径的结果；`memory_save` 写入后缓存立即失效
（归档写入的回合要等 TTL 或换 query 才会出现在召回里）。窗口内断连由读写失败重试兜底
（实测：会话被服务端掐断后，下一条语句自动换连接并成功）。

## 验证

```bash
journalctl -u astrbot -f | grep atbrains
#   [atbrains] 已注入召回 N 条（qq-alt:GroupMessage:...）
#   [atbrains] 已归档回合 #id（qq · qq-alt:GroupMessage:...）
python3 main.py --selftest        # 只读自检，不改数据
#   AstrBot 机器上要用 AstrBot 自带 venv 的解释器（astrbot 包只装在那里）：
#     /opt/astrbot/venv/bin/python main.py --selftest
#     用系统 python3 会打「未检测到 AstrBot 运行环境，使用占位实现」——自检仍可跑，
#     但那个解释器里钩子不会注册，别拿来跑机器人。
#   没有 AstrBot 的机器（预检 / 排障）：python3 main.py --selftest 现在也能跑，
#     模块顶部对 astrbot 是软导入，失败时退化为占位实现并打印上面那行警告。
```

落库侧：

```sql
SELECT id, kind, LEFT(content,40), created_at
FROM brain_memories WHERE agent = 'astrbot' ORDER BY id DESC LIMIT 5;
```

## 注意

- **工具白名单**：人设的 `tools` 必须包含 `memory_save` / `memory_search`，否则
  AstrBot 不会把这两个函数注入请求，模型自然调不到。
- **pymysql ≥ 1.1 的 `ping(reconnect=True)` 已废弃**：本插件在 `_mysql()` 里手动建连，
  `ping()` 按 `mysql_ping_interval`（默认 30 s）节流，读写失败时降级重连一次
  （被服务端掐断的陈旧连接会在下一条语句上自愈）。
- **归档不挡回合收尾**：`archive_async=1`（默认）时归档在后台任务里跑。进程被强杀
  （如 `systemctl restart`）时，正在跑的那一轮可能少一行 `brain_turns`，其余轮次不受影响。
- **QQ 出站干净**：非流式响应里 `chain.type == "reasoning"` 的推理文本与工具状态消息
  由 `display_reasoning_text` / `show_tool_use_status` / `show_tool_call_result` 控制，
  默认全关；webchat 的 SSE 会实时显示这些中间内容，属设计行为。
- 运行时数据（`data/`、`__pycache__/`）不入库。
