# atbrains 插件隐藏缺陷审计

- 审计基线：commit `402861c`（`core.py` 2556 行 / `__init__.py` 520 行 / `cli.py` 433 行）。
- 行号均为**审计基线**行号；修复处的当前行号见文末「修复清单」。
- 严重度：P0 = 会丢数据 / 写错库；P1 = 边界条件出错、并发/失败路径会产生脏数据；P2 = 可维护性或确有风险但不改。
- 已修复项在「修复方式」里注明「已修复」，未修复项统一在文末「未修复项及原因」说明。

## 问题清单

| 严重度 | 文件:行 | 问题 | 触发条件 | 修复方式 |
|---|---|---|---|---|
| P0 | `core.py:988-1035` `store_turn_messages` | 游标 `archived_messages` 是「先 SELECT、再 INSERT 消息、最后 UPDATE 游标」三步，无锁无事务。两个进程/线程同时同步同一 session 时都从同一 `done` 起写 → 同一段对话重复落库；进程在 INSERT 之后、UPDATE 之前退出 → 下次重复灌入；`brain_sessions` 行缺失时 UPDATE 命中 0 行且不报错，游标永不推进 | 网关 + CLI/cron 并发 sync 同一会话；写转录途中崩溃；会话行被 prune/手工删掉后继续 sync | **已修复**：会话级事务 + `SELECT ... FOR UPDATE` 行锁，先 `INSERT ... ON DUPLICATE KEY UPDATE` 保证会话行存在，消息与游标同一事务提交，异常回滚 |
| P0 | `__init__.py:481` `get_config_schema` | `mariadb_db` 默认值是 `agent_memory`，而实际库、README、`DEFAULT_CONFIG` 是 `agent_memory`。setup 向导会把错误库名写进 `atbrains.json` | `hermes memory setup` / 配置向导保存（值来自 schema 默认值） | **已修复**：默认值改为 `agent_memory` |
| P1 | `core.py:1203` `recent_turns` | DB 回退路径把库内 naive DATETIME（UTC）直接 `.timestamp()`，Python 会按**本机时区**解释 → 非 UTC 主机（Windows 桌面版、`TZ=Asia/Shanghai`）时间整体偏移，CLI 再 `localtime()` 又偏一次 | 主机时区不是 UTC 且 Redis 不可用/`source="db"` | **已修复**：新增 `_utc_epoch()`，naive 值显式按 UTC 解释 |
| P1 | `core.py:1464` `_mark_vector` | `embedded_at` 用 `time.strftime()`（本机时区）写入，而库内时间统一 UTC；`vector-status` 的 `last_ok` 在非 UTC 主机上错位 | 非 UTC 主机完成向量化 | **已修复**：改用 `datetime.now(timezone.utc)` |
| P1 | `core.py:166-195` `_dotenv_get` | 不处理行内注释：`KEY=pw # prod` 会把 ` # prod` 当密码；`KEY="pw"  # prod` 因末字符不是引号而整段返回；单引号值同理 | `.env` 里密码/令牌后面跟注释（常见手写） | **已修复**：新增 `_dotenv_value()`，先解析引号（含 `\"`/`\\`），未加引号时仅在空白后的 `#` 处截断 |
| P1 | `__init__.py:135-166` `is_available` / `unavailable_reason` | 远程模式（`remote_enabled=1` + `remote_url`）只需要 HTTP 令牌，但可用性检查强制要求本机 Redis/MariaDB 密码 → 没有本地库的客户端永远无法加载 provider，远程模式形同虚设 | 纯远程客户端机器（无本地 Redis/MariaDB 凭据） | **已修复**：远程模式配置完整时直接可用；`unavailable_reason` 同步跳过本地凭据检查 |
| P1 | `__init__.py:279-305` `on_memory_write` | `replace` 先 `deactivate_by_content(prev)` 再 `remember(content)`。若第二步失败（库抖动、内容被拒），旧记忆已被停用、新记忆没写进去 → 净丢失 | replace 写入时数据库短暂不可用 | **已修复**：先写新内容，成功后再停用旧内容；新旧内容相同时只做 dedupe 刷新、不停用 |
| P1 | `core.py:776-780` `remember` | `(content or "").strip()` 对 list/dict 直接 `AttributeError`，不是 `BrainError`；调用方（hook/工具边界）只看到模糊异常 | 外部传入非字符串 `content`（list/ multimodal / dict） | **已修复**：先 `str()` 再校验；`deactivate_by_content` 同样处理 |
| P1 | `core.py:593-608`（基线）`_exec` / 转录写入 | 参数里的 lone surrogate（如 JSON 里的 `"\ud800"`）无法被 PyMySQL 编码成 utf-8，抛 `UnicodeEncodeError` → 整条 INSERT（整轮对话/整条记忆）失败，hook 只记 warning | 模型/工具输出含非法 UTF-16 代理字符 | **已修复**：新增 `_db_safe()`（`core.py:564`），`_exec`（`core.py:666`）与 `store_turn_messages`（`core.py:1042`）写入前清洗；仅对无法编码的 str 做 `encode(utf-8, replace)` 兜底 |
| P1 | `cli.py:95-102` `_cmd_remember` | 远程模式成功返回的 `saved` 由服务端决定，可能没有 `hot_cache`；CLI 直接 `result['hot_cache']` → 写入已成功却 KeyError 退出 | `remote_enabled=1` 时 `hermes atbrains-memory remember` | **已修复**：全部改 `.get()`，并把远程明确失败时的 `error` 打出来 |
| P1 | `core.py:2179` `_distill_tick` | `--now --turns N` 取「最近 N 轮」，随后把水位直接推到这批最大 id。若水位与这批之间还有积压轮次，它们被**永久跳过**（自动线程再也不会蒸馏） | 长时间停机后手动 `hermes atbrains-memory distill --now` | **已修复**：仅当水位与手动批次之间没有更老的未处理轮次时才推进水位；有 gap 时保持原水位（重复批次由内容 hash 去重兜底） |
| P1 | `core.py:907-921` `record_turn` | `remote_turn_sync=1` 时只 POST `session_id/user/assistant/platform`，本地路径直接 return：`messages` 全量转录层**静默丢失**（服务端接口不接收该字段） | 打开 `remote_turn_sync` 且依赖 `atbrains_turn_detail` | **未修复**（需要 Memory Service 接口契约，见文末） |
| P1 | `core.py:855-878`、`1139-1174` | `deactivate_by_content` 与 `search_memories/_hybrid` 不感知远程模式：远程模式写的是权威库，`on_memory_write` 的 remove/replace 却在本地停用（无效），`atbrains_recall`/`prefetch` 本地检索可能读不到服务端数据 | `remote_enabled=1` | **未修复**（客户端没有对应远程端点，见文末） |
| P1 | `core.py:992-997` | 压缩检测只看条数：`done > len(msgs)` 才回退。若压缩/重写后条数不减少（摘要 + 保留尾部），索引错位，尾部新增消息可能被当作已归档而**跳过** | Hermes 用等长/更长摘要重写历史 | **未修复**（需要内容指纹，见文末） |
| P1 | `core.py:489-495` | v4 迁移回填 `memory_type` 只映射 `builtin-memory/fact/decision`，漏了 `insight`、`preference`（`_LAYER_BY_KIND` 里两者是 `knowledge`）→ 老数据被标成 `episodic`，参与时间衰减 | 从 v3 升级上来且库里已有 `insight/preference` | **未修复**（工单禁止写迁移；已升级库改语句也无效） |
| P2 | `core.py:623-651` `ensure_schema` | `GET_LOCK` 没拿到（超时/为 0）也继续跑迁移并写 `schema_version`；并发 DDL 只靠 `IF NOT EXISTS` + 容忍 errno 兜底 | 多进程同时首次启动 | 不改（迁移语句本身幂等；改动风险高于收益） |
| P2 | `core.py:1268-1291` `_compose` | `prefetch_max_chars<=0` 时返回 `" …"` 这种非空垃圾注入文本 | 配置为 0/负数 | 不改（配置校验问题，默认 1200） |
| P2 | `core.py:767-771` `_hot_put` | ZSET 只按 rank 淘汰索引，`brain:hot:<id>` 本体留到 TTL（30 天）才过期；高频写入时 Redis 里会积压无索引引用 | 大量 remember + LRU 淘汰 | 不改（有 TTL 兜底，非数据错误） |
| P2 | `core.py:1080-1081` `turn_detail` | `tool_calls` 原样返回，不受 `max_chars` 限制，单轮可能返回巨大 JSON | 工具调用参数极大 | 不改（读取侧限流是产品选择） |
| P2 | `core.py:1002,1031` `store_turn_messages` | `seq` 是 `SMALLINT UNSIGNED`，单轮 delta > 65535 条时整批 INSERT 失败；游标不推进 → 每轮重试都失败 | 极端长会话一次补交 >65535 条 | 不改（需要改 schema/分批，工单禁止改表） |
| P2 | `core.py:1114-1128` `archive` | 400k 字符处硬截断，可能把 JSON 切成非法片段并追加 `[truncated]` | 压缩前存档 >400KB | 不改（有界存储的产品选择，读取侧只做展示） |
| P2 | `__init__.py:307-330` `on_pre_compress` | 每条消息截 2000 字符后再存档；存档层本意是「防细节丢失」，超长消息细节仍会丢 | 单条消息 >2000 字符 | 不改（无上限会撑爆 `brain_archive`，需产品决策） |
| P2 | `core.py:904-974` `record_turn` | `session_id` 为空时落到唯一的 `session_id=''` 行，所有匿名会话共享同一个 `archived_messages` 游标 → 转录互相跳过 | 调用方不传 session_id | 不改（Hermes 钩子始终有 session_id；改语义会影响空串会话） |
| P2 | `core.py:2114-2138` `_distill_tick` | 被过滤的行（空 user / `[IMPORTANT` 开头）不计入 `rows`，若它们是队尾，水位会停在其前面，每轮重复扫描 | 队尾恰好是系统注入轮次 | 不改（不会丢数据，只多一次空扫；推进水位需要区分「已处理」） |
| P2 | `__init__.py:241-266` `queue_prefetch` | 每次调用起一个预热线程，无并发上限；慢查询/远程抖动时线程堆积 | 高频 `queue_prefetch` | 不改（线程短暂且 Hermes 调用频率有限） |
| P2 | `__init__.py:180-211` `_start_vector_drainer` | drainer 是 **provider 实例**级，不是进程级；多个 provider 实例（子代理）会各起一个 drainer | 同时存在多个 provider 实例 | 不改（`drain_vectors` 幂等 + GET_LOCK 兜底） |
| P2 | `__init__.py:484-507` `save_config` | 读-改-写 `atbrains.json` 无锁，两个进程同时保存会丢其中一份非密配置 | 并发 setup/保存 | 不改（非密配置，人工操作冲突概率低） |
| P2 | `cli.py:143-147` `_cmd_vector_drain` | `--max -5` 被 `max(0, ...)` 变成 0，语义从「负数」变成「不限量」 | 手误传负数 | 不改（CLI 参数边界，无数据风险） |
| P2 | `__init__.py:430-443` `_tool_recent` | `what` 未知枚举值不报错，静默按 `turns` 返回 | LLM 传 `what="memory"` 等 | 不改（保持宽松兼容，字段名不变） |
| P2 | `__init__.py:409` `_tool_recall` | `include_turns` 直接用真值判断；JSON 里若传字符串 `"false"` 仍为真 | 客户端传字符串而非布尔 | 不改（工具 schema 已声明 boolean） |
| P2 | `core.py:1176-1196` `recent_turns` | `limit` 未做正数校验，负数会直接进 Redis `xrevrange(count=…)` / SQL `LIMIT` | 内部调用方传负数 | 不改（工具/CLI 均已 clamp；核心 API 保持宽松） |
| P2 | `core.py:2492-2556` `prune` | 单条 DELETE 无 LIMIT，大库清理可能超过 `read_timeout=10s`；客户端报错但服务端可能仍在执行 | 库很大时手动/定时 prune | 不改（需要分批清理 + 断点，属于功能改造） |
| P2 | `__init__.py:270-276` `sync_turn` | 写失败只 `logger.warning`，没有持久化重试队列；`record_turn` 已自动提交轮次，盲目重试会重复轮次 | 数据库瞬时不可用 | 不改（按设计 fail-open；重试需幂等键，工单禁止改表） |

## 修复清单

所有改动均保持 3.9+ 语法、未改公开函数签名、未动 `vendor/`、未改表结构/迁移、未改工具名与 JSON 字段名。
每处代码都带一句「为什么」注释。

| # | 文件:行（修复后） | 修改 | 对应问题 |
|---|---|---|---|
| 1 | `core.py:1042` `store_turn_messages` | 改为「会话行 upsert → `SELECT ... FOR UPDATE` → 写消息 → 推游标」同一事务，异常回滚；游标切片抽成纯函数 `_cursor_start`（`core.py:579`） | P0 游标并发/崩溃窗口、会话行缺失导致游标不推进 |
| 2 | `__init__.py:499` `get_config_schema` | `mariadb_db` 默认值改为 `agent_memory` | P0 setup 把库名写成 `agent_memory`（写错库） |
| 3 | `core.py:343` `_utc_epoch`；`core.py:1261` `recent_turns` | DB 回退路径用 `_utc_epoch()`，naive DATETIME 显式按 UTC 解释 | P1 非 UTC 主机时间整体偏移 |
| 4 | `core.py:1548` `_mark_vector` | `embedded_at` 改用 `datetime.now(timezone.utc)` | P1 `vector-status.last_ok` 错位 |
| 5 | `core.py:166` `_dotenv_value`；`_dotenv_get` | 统一解析引号与行内注释（仅空白后的 `#` 截断） | P1 `.env` 密码/令牌被注释污染 |
| 6 | `__init__.py:37` `_remote_configured`；`is_available` / `unavailable_reason` | 远程模式（开关 + URL）不再要求本机 Redis/MariaDB 密码 | P1 纯远程客户端无法加载 provider |
| 7 | `__init__.py:308` `on_memory_write` | `replace` 先写新内容、成功后再停用旧内容；内容相同不停用 | P1 replace 失败导致旧记忆净丢失 |
| 8 | `core.py:841` `remember`；`core.py:920` `deactivate_by_content` | 非字符串 content 先 `str()` 再处理 | P1 list/dict content 抛 `AttributeError` |
| 9 | `core.py:564` `_db_safe`；`_exec`（`core.py:666`）与转录写入 | bound 参数里的 lone surrogate 替换为 `?`，正常字符串零改动 | P1 非法 UTF-8 导致整条写入失败 |
| 10 | `cli.py:95` `_cmd_remember` | 结果字段全部 `.get()`；远程明确失败时退出码 1 | P1 远程写入成功后 CLI KeyError |
| 11 | `core.py:2266` `_distill_tick` | 手动 `--now` 与旧水位之间有积压时**不推进**水位；水位永不回退 | P1 手动蒸馏永久跳过积压轮次 |
| 12 | `README.md` | 同步「手动蒸馏水位例外」与「库内时间按 UTC 解释」两处描述 | 文档与修复后行为一致 |

## 验证

- `python3 -m py_compile core.py __init__.py cli.py selftest.py`：通过。
- `python3 selftest.py`：25 个纯函数用例全部通过（检索词、LIKE 转义、截断、掩码、分层、UTC/年龄、
  余弦、`.env` 解析、`resolve_config` 合并与类型、游标切片、surrogate 清洗、errno 判定、
  `_compose` 预算、`_rk` 前缀、`_fmt_bytes`）。
- 额外用假连接（`FakeConn`/`FakeCursor`，不连库）跑过 `store_turn_messages` 四条路径：正常增量、
  压缩回退、空增量、写消息抛错；确认 `BEGIN/COMMIT/ROLLBACK` 次数、`FOR UPDATE` 存在、
  游标只在提交时推进、失败必回滚。
- 未连接 MariaDB/Redis/Qdrant，未联网，未写入 `/root/.hermes`。

## 未修复项及原因

### P1

1. **`remote_turn_sync=1` 时全量转录层丢失**（`core.py:974-989`）。
   远程端点只接收 `session_id/user/assistant/platform`，`messages` 没有字段可送；本地路径又直接
   return。修复要么改 Memory Service 接口（不在本工单范围/无法在客户端单方面保证服务端兼容），
   要么在远程模式下另写一份本地 turn+转录（会让 `turn_id` 语义分裂，且与「远程模式不写本地权威库」
   的设计冲突）。风险：打开该开关的机器不能用 `atbrains_turn_detail` 复盘工具过程。

2. **远程模式下 `deactivate_by_content` / `search_memories(_hybrid)` 仍访问本地库**（`core.py:922-945`、`1224-1259`、`1710`）。
   客户端没有 `/v1/search`、也没有「按内容废止」的远程端点；擅自复用 `/v1/memory/forget`（按 id）
   需要先远程检索拿到 id，语义与失败回落都会变复杂。风险：远程模式下 `on_memory_write` 的
   remove/replace 只停用本地镜像（服务端旧记忆仍在），`atbrains_recall` 可能读不到服务端新记忆。

3. **压缩后条数不减少时的游标误判**（`_cursor_start`，`core.py:579-596`）。
   现在只在 `done > len(msgs)` 时回退。若 Hermes 用等长/更长的摘要重写历史，索引整体错位，
   尾部新增消息可能被当成已归档而跳过。可靠修复需要「上一条已归档消息的内容指纹」，可选存放点
   只有 `brain_sessions.note`（复用会与运维备注冲突）或新增列（工单禁止改表）。风险：特定压缩
   实现下静默少存转录；主档 `brain_turns` 与 Redis 流不受影响。

4. **v4 迁移回填漏掉 `insight` / `preference` 分层**（`core.py:494-535`）。
   `_LAYER_BY_KIND` 把这两类映射为 `knowledge`，迁移 CASE 只写了 `fact/decision`。工单禁止写迁移，
   且改语句对已升级库无效。风险：老库里这两类记忆被标成 `episodic`，参与时间衰减。
   建议运维在确认后手工执行（本次未执行）：
   `UPDATE brain_memories SET memory_type='knowledge', updated_at=updated_at WHERE memory_type='episodic' AND kind IN ('insight','preference');`

### P2

上表 P2 行按工单要求**只记录不修**。其中最需要留意的两条：

- `seq SMALLINT UNSIGNED` 在单轮 >65535 条消息时会让整批转录写入失败（需改表/分批写）。
- `prune` 单条 DELETE 无 LIMIT，大库清理可能超过 `read_timeout=10s`，客户端报错但服务端可能仍在执行
  （需要分批清理与断点，属于功能改造）。

另有两项是既有 fail-open 设计的固有风险，未改动：远程写入超时后回落本地可能产生「服务端已提交 + 本地再写」
的双写；`sync_turn` 失败只告警、没有持久化重试队列（盲目重试会重复 `brain_turns` 行）。
