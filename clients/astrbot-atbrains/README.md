# astrbot-atbrains — AstrBot 的 atbrains 长期记忆插件

把 AstrBot（QQ 机器人框架）接入 atbrains 记忆库：每轮对话自动召回相关记忆、
提供 `memory_save` / `memory_search` 函数工具、每轮问答自动归档到
`brain_turns` / `brain_sessions` 与 Redis `brain:recent`。写入方身份固定为
`agent = 'astrbot'`，与 Hermes、dsh 等 agent 共享同一个 MariaDB 库。

## 能力

| 钩子 | 行为 |
|---|---|
| `filter.on_llm_request()` | 按本轮用户消息做关键词召回（长期记忆 + 历史对话片段），拼到 `req.system_prompt`；用 `event.set_extra` 防止同一轮重复注入。检索失败只降级不抛错 |
| `filter.on_agent_done()` | 归档本轮问答到 `brain_turns` / `brain_sessions`，并 append 到 Redis `brain:recent` |
| `filter.llm_tool` | 注册 `memory_save` / `memory_search` 两个工具（docstring 里的 `Args:` 必须带 `(string)` / `(number)` 类型标注，否则 AstrBot 解析不出参数 schema） |

插件对记忆库只做「读记忆 / 写记忆 / 写回合」三类操作：**不写转录层、不做向量化**
（向量回填由 Hermes 侧的 drainer 负责）。

## 目录

```
main.py         插件主体（约 620 行，单文件）
metadata.yaml   AstrBot 插件元信息
vendor/         pymysql + redis 源码（不走 pip，绕开国内装包）
```

`vendor/` 是源码内置依赖：`main.py` 启动时把 `vendor/` 插到 `sys.path` 前面，
因此不需要在 `/opt/astrbot/venv` 里额外 `pip install`。

## 安装

```bash
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

## 验证

```bash
journalctl -u astrbot -f | grep atbrains
#   [atbrains] 已注入召回 N 条（qq-alt:GroupMessage:...）
#   [atbrains] 已归档回合 #id（qq · qq-alt:GroupMessage:...）
python3 main.py --selftest        # 只读自检，不改数据
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
  读写失败时降级重连一次。
- **QQ 出站干净**：非流式响应里 `chain.type == "reasoning"` 的推理文本与工具状态消息
  由 `display_reasoning_text` / `show_tool_use_status` / `show_tool_call_result` 控制，
  默认全关；webchat 的 SSE 会实时显示这些中间内容，属设计行为。
- 运行时数据（`data/`、`__pycache__/`）不入库。
