# Agent 记忆系统 · 配置总览

> 生成 2026-10-12 · 更新 2026-10-15 · 维护者：服务器上的 Hermes agent（会话中可让 agent 直接读改本文件）
> 密码等机密**不在本文件**：见 `/root/agent-db/credentials.txt`（600 权限）
> 配套材料在 `/root/agent-db/`（重建命令 / 初始化 SQL / 台账 / 自检脚本）；维护手册见 skill `hermes-brain-memory`

## 1. 总架构（短期 + 长期 双层记忆）

| 层 | 载体 | 端口 / 数据卷 | 存放内容 | 生命周期 |
|---|---|---|---|---|
| 短期 | Redis 容器 `redis` | 0.0.0.0:9079 · 卷 `redis_data` | 最近对话流 `brain:recent`、会话状态 `brain:session:*`、热点记忆 `brain:hot:*`、检索缓存 `brain:qcache:*` | TTL 自动过期（14~30 天），无需维护 |
| 长期 | MariaDB 容器 `mariadb` | 0.0.0.0:57306 · 卷 `mariadb_data` | `brain_memories`（事实/偏好/决策）、`brain_turns`（对话归档）、`brain_sessions`（会话索引）、`brain_archive`（压缩前存档） | 永久（`brain-prune.timer` 每周自动 prune：对话全量保留；sessions/archive/inactive 365 天） |
| 向量 | Qdrant 容器 `qdrant` | 127.0.0.1:6333 · 目录 `/root/qdrant` | 记忆向量集合 `brain_memories`（2560 维 / Qwen3-Embedding-4B / SiliconFlow） | 状态表 `brain_vector_index` 跟踪；软删记忆的向量随 drain 自动清理 |

- 两个容器均 `--restart unless-stopped`；端口已去掉 127.0.0.1 限制（绑 0.0.0.0 双栈）。2026-10-13 外部多点实测：腾讯云安全组已放行 57306/9079，公网直连可达
- MariaDB 服务器参数：`utf8mb4` / `utf8mb4_unicode_ci`
- **时区：库内统一 UTC（+00:00）**（`mysql-conf.d/99-timezone.cnf` 固定 default-time-zone，重建脚本已挂载）。2026-10-13 曾短暂切至 +08:00，同日按用户要求回退（数据已 -8h 还原；2026-10-14 全库逐行核验无错误、brain_turns 已按时间重排重编号）。注意：展示脚本不做换算，看库时以 UTC 为基准（比北京时间慢 8 小时；需要本地时间可用 `CONVERT_TZ(ts,'+00:00','+08:00')`）
- 所有表名 `brain_*`；三张主表均带 `machine` 列（来源机标识，服务器= `server`）

## 2. 命名空间分配（多 agent 台账）

| 使用方 | MariaDB 库 | MariaDB 用户 | Redis DB | 键前缀 |
|---|---|---|---|---|
| hermes（服务器，我） | `hermes_agent_memory` | `agent` | db0 | `brain:` |
| hermes@x260-debian（linux 笔记本，已接入；2026-10-13 由 x260 更名） | `hermes_agent_memory`（共享） | `agent` | db1 | `brain:` |
| hermes@windows-laptop（桌面版，已接入） | `hermes_agent_memory`（共享） | `agent` | db2 | `brain:` |
| hermes@新实例（2026-10-13 交付工单，接入中） | `hermes_agent_memory`（共享） | `agent` | db3 | `brain:` |
| hermes 预留（新实例空位） | `hermes_agent_memory`（共享） | `agent` | db4 | `brain:` |
| omp | `omp_agent_memory` | `omp` | db5 | `omp:` |
| dsh | `dsh_agent_memory` | `dsh` | db6 | `dsh:` |
| artex | `artex_agent_memory` | `artex` | db7 | `artex:` |

规则：每 agent 独立库 + 同名用户（`GRANT ALL` 仅限本库）；Redis 同密码下按"库号 + 前缀"约定隔离，db0-db4 预留给 hermes 系（一台一库，新实例取空位），非 hermes agent 从 db5 起顺延；新 agent 顺延 `<name>_agent_memory` / 用户 `<name>` / dbN / 前缀 `<name>:`；命名一律小写 ASCII，弃用名不复用（`agent_memory` 为墓碑名）。

## 3. 记忆实现（brain 插件）

- 插件目录：`/root/.hermes/plugins/brain/`
  - `__init__.py` 记忆提供者（Hermes 钩子接入）；`core.py` 存储引擎；`cli.py` 命令入口
  - `vendor/` 内置纯 Python 的 redis-py + PyMySQL（**勿**改成 pip/PM 管理，见插件 README）
  - `skills/brain/SKILL.md` → 会话内技能 `brain:memory`
- 激活方式：`memory.provider: brain`（`hermes config set memory.provider brain`）
- 非密配置 `~/.hermes/brain.json`，当前内容：
  ```json
  {
    "redis_host": "127.0.0.1", "redis_port": 9079, "redis_db": 0,
    "mariadb_host": "127.0.0.1", "mariadb_port": 57306,
    "mariadb_user": "agent", "mariadb_db": "hermes_agent_memory",
    "machine": "server",
    "vector_enabled": 1, "embed_model": "Qwen/Qwen3-Embedding-4B", "embed_dim": 2560,
    "qdrant_url": "http://127.0.0.1:6333", "qdrant_collection": "brain_memories",
    "vector_topk": 12, "vector_min_score": 0.25
  }
  ```
- 密码：`~/.hermes/.env` 中的 `BRAIN_REDIS_PASSWORD` / `BRAIN_MARIADB_PASSWORD`
- 自动行为：每轮对话前召回注入（片段带 `[来源机]` 标注）、每轮完成后双写归档、内置 MEMORY.md/USER.md 写入自动镜像、上下文压缩前存档、子代理结果入库
- 结构 v2（2026-10-13）：`brain_meta.schema_version` 记录结构版本，插件启动时自动幂等迁移。v2 新增：活跃行防重唯一键（同内容全库只留一条，重复写入自动改为刷新既有行）、召回索引 `idx_recall`、`deactivated_at` 停用时间、`brain_turns.truncated` 截断标记、`brain_archive.machine/platform`、CHECK 约束
- 结构 v3（2026-10-14 深夜）：新增 `brain_vector_index`（向量索引状态表，Schema v3）。向量层 key 存放 = qdrant 配置文件（`/root/qdrant/qdrant-config/production.yaml`，不进 Hermes 配置）
- 语义召回（向量层，2026-10-14 深夜上线）：写入后台异步向量化（drainer+wake，跨进程锁）→ Qdrant；检索=关键词+向量 RRF 混合、失败自动回落纯关键词；开关 `vector_enabled`
- 检索口径：对话检索带时间窗（`search_turns_days`，默认 120 天，可在 brain.json 调整）；归档数据不受影响
- 工具：`brain_remember` / `brain_recall`（语义+关键词混合） / `brain_forget` / `brain_recent` / `brain_stats`
- CLI：`hermes brain status|doctor|search|remember|recent|forget|prune|vector-status|vector-drain|vector-test`
- 浏览（「最新在前」口径，2026-10-13 定稿）：库内视图 `brain_memories_newest` / `brain_turns_newest` / `brain_sessions_newest` —— 直接 SELECT 即是最新在前（实测带 WHERE 过滤也保持）；全库总览与快照 `python3 /root/agent-db/brain-overview.py [--all] [--machine X] [--out FILE]`（快照默认写 brain-overview.txt）；machine 标签修正后用 `/root/agent-db/refresh-hot-machine.py` 校正 Redis 热点镜像。注意：召回注入保持「重要度优先、同重要度最新在前」——浏览与召回是两套口径

## 4. 容器与数据安全

- 保数据重建容器：`/root/agent-db/rebuild.sh`（优雅停 → 删 → 原参数重建，数据卷不动）
- 空卷从零初始化：`/root/agent-db/init.sql`（自动建 omp/dsh/artex 的库与账号；hermes 库由容器 env 创建）
- 迁移前备份：`/root/agent-db/backup-agent_memory-20261012.sql`
- 结构 v2 升级前全量备份：`/root/agent-db/backup-hermes_agent_memory-20261013-pre-v2.sql`（945KB）
- ⚠️ `rebuild.sh` / `credentials.txt` / `init.sql` 由 `generate-artifacts.py` 生成（模板内嵌其中）——手改后必须同步改生成器模板，否则下次生成会被覆盖
- 时区配置：`mysql-conf.d/99-timezone.cnf`（固定 UTC +00:00；重建脚本已挂载，运行中容器内同路径文件亦在）
- 账号自检：`python3 /root/agent-db/verify-accounts.py`
- 关键事实：`MARIADB_*` 环境变量只在**空卷首次初始化**时生效；日常改数据直接改库，无需重建容器

## 5. 运维速查

```bash
hermes memory status          # 记忆提供者状态
hermes brain doctor           # 依赖/凭据/连接/表结构自检
hermes brain status           # 连通性 + 计数
hermes brain search <关键词>   # 检索（记忆 + 历史对话）
hermes brain remember "内容" --kind note --tags xx --importance 6
hermes brain recent --n 10    # Redis 最近对话流
hermes brain prune --dry-run  # 清理预演（turns 默认 0=不清理；sessions/archive/inactive 365 天）
hermes brain vector-status    # 向量层状态：开关/模型/key 来源/已向量化统计
hermes brain vector-drain     # 手动补向量（--force 全量重嵌；改模型后需要）
hermes brain vector-test "查询" # 关键词/向量/融合 三方对比
journalctl -u brain-vector-drain.service -n 30   # 兜底定时器日志（每3分钟自动回填）
systemctl list-timers brain-prune.timer   # 每周自动 prune；日志 journalctl -u brain-prune.service
python3 /root/agent-db/audit-turns.py         # 完整性审计：state.db ↔ brain_turns 对账
python3 /root/agent-db/audit-turns.py --apply # 同上，发现缺失即回填
python3 /root/agent-db/brain-overview.py      # 全库总览（记忆/对话/会话，最新在前；--out 写文件）
```

- 数据快照（2026-10-13 下午 · 浏览口径最新在前）：记忆 43 条活跃 / 61 总、对话归档 510+ 条、会话 143 个（最新数见 brain-overview.txt）
- 网关：新会话自动启用；大改动后 `hermes gateway restart` 最干净
- 微信平台有 iLink token ~15 分钟限制，网关重启通知可能发不出去（已知现象）

## 6. 多设备共享（已启用：服务器 + x260-debian + windows-laptop）

三台 Hermes（服务器 / linux 笔记本 / windows 笔记本）共享 `hermes_agent_memory` 长期库：

- 长期记忆互通；Redis 短期层各用各库号（db0=服务器、db1=x260-debian、db2=windows-laptop、db3=新实例（10-13 交付）；db4 留给后续新实例；非 hermes agent 在 db5 起）；召回片段自动标注来源机
- 纪律：新事实用 `brain_remember`（三机共享）；内置 MEMORY.md 以服务器为"主"维护（各机文件不自动同步）
- 网络（2026-10-13 定稿）：笔记本直连公网 111.230.71.79（安全组已放行 57306/9079，外部多点实测可达）；EasyTier 路线作废（组网里只有服务器与路由器两个节点，开发机不在网内）
- 笔记本接入 5 步：① 复制 `/root/.hermes/plugins/brain/` 整目录（含 vendor） ② `brain.json` 改服务器地址（公网 111.230.71.79）、`redis_db` 填分配库号（x260-debian=1、windows-laptop=2；新实例取空位——db3 已交付，下一空位 db4）、`machine` 设标签 ③ `.env` 写两个密码变量 ④ `hermes config set memory.provider brain` ⑤ `hermes brain doctor` + 聊天验证 🧠

## 7. 变更大事记（2026-10-12 ~ 10-13）

1. brain 双存储记忆接入（Redis 短期 + MariaDB 长期），核心测试（读写/召回/归档/工具/CLI）全通过
2. 内置记忆 27 条迁入数据库（MEMORY.md 15 条 + USER.md 12 条，逐字节一致）
3. 多 agent 命名空间分离：`hermes_agent_memory` + omp/dsh/artex 三库，跨库迁移零丢失（30/11/5/0 前后一致）
4. `machine` 来源机标识加固：三表加列 + 插件自动升级 + 召回标注 + 台账登记笔记本 db4/db5
5. 两个容器保数据重建（去掉 127.0.0.1 端口限制，数据卷原样挂回）
6. 完整性审计与修复：回填 c59721 会话丢失的 7 轮对话（激活前 3 轮 + 库改名窗口写入失败 4 轮）、补回 1 条镜像记忆（17:14 写入失败）；新增审计脚本 `/root/agent-db/audit-turns.py`
7. Redis 库号重排：db0-db4 预留给 hermes 系（x260 自 db4 迁至 db1，存量 10 键无损随迁）；omp/dsh/artex 顺延至 db5/6/7
8. （2026-10-13）网络定稿：多设备统一公网直连 111.230.71.79；腾讯云安全组 57306/9079 放行经外部多点实测确认；EasyTier 路线作废
9. （2026-10-13）存量空 machine 标签回填为 server（记忆 30 / 对话 12 / 会话 5 行，updated_at 保持原值未扰动）；「最新在前」浏览就位：`brain_*_newest` 三视图 + `/root/agent-db/brain-overview.py`（快照 brain-overview.txt）；Redis 热点镜像 19 条 machine 校正、旧检索缓存清理
10. （2026-10-13）结构 v2 升级（防重唯一键 / 召回索引 / deactivated_at / 截断标记 / 存档来源列 / CHECK；brain_meta 版本化自动迁移）+ 时区调整：曾切至 +08:00，同日按用户要求回退为 UTC（数据往返平移已还原；audit 回填口径=UTC）+ `brain-prune.timer` 每周自动清理 + 升级前全量备份；插件分发包更新为 `brain-plugin-20261013`（.tar.gz/.zip，位于 dist/）
11. （2026-10-13）机器标签更名：x260 → x260-debian（库内四表 machine 全量更名 memories 7 / turns 82 / sessions 33 行，旧标签清零；Redis db1 热点镜像同批校正；x260 本机 brain.json 更名后新写入才带新标签）
12. （2026-10-14）时间核验 + brain_turns 整理：全库 542 轮逐行对账（server↔state.db、各机↔Redis 流 epoch）确认无任何 ±8h 错误（口径=UTC）；`brain_turns` 按 ts 全表重排、连续重编号 1..N（旧编号仅存于 `backup-brain_turns-20261014-pre-rebuild.sql`，已做恢复校验；库内备份表 `brain_turns_bak_20261014` 已于当日验证后删除；再整理脚本 `/root/agent-db/reorder-turns.sh`）；`brain_sessions`/视图已同步；`brain_archive` 现有 1 行为正常（仅上下文压缩时写入；来自 10-13 晚 x260-debian 长会话，machine/platform 已补正）
13. （2026-10-14 晚）brain_turns 复核与二次重排：按用户要求全表逐行复核——原表本就是严格时间序（0 处倒挂）；重排前后逐行校验零差异（新旧 CRC32 一致、550 行全覆盖）；重排脚本升级 v2（时间戳 dump、无库内 bak 残留、old_tmp 回滚、orig_id 漂移追踪、dump 恢复自检）；本次备份 `backup-brain_turns-20261014-205158-pre-resort.sql`
14. （2026-10-14 深夜）brain_turns id 跳变修复：v2 重排的批量 `INSERT...SELECT` 触发 InnoDB auto_increment 按 2 的幂预留（550 行 → 计数器停在 1024，用户报「id 突然跳到1024」）；沙箱复现后重排脚本升级 v3（显式 `ROW_NUMBER` 行号插入 + `next_id==MAX(id)+1` 阻断校验），全表重排归位：id 1..551 连续、next=552、内容零变化（CRC32 一致）；备份 `backup-brain_turns-20261014-223310-pre-resort.sql`
15. （2026-10-14 深夜）**语义召回（向量层）上线**：Qdrant 容器部署（v1.19.2，数据 `/root/qdrant`，访问口令=qdrant 配置文件 `/root/qdrant/qdrant-config/production.yaml`（service.api_key，只读挂载进容器；按用户硬要求不落 Hermes 配置））；brain 插件 v1.1.0：写入异步向量化（drainer+wake+跨进程 `GET_LOCK`）→ SiliconFlow `Qwen/Qwen3-Embedding-4B`（2560 维）→ Qdrant 集合 `brain_memories`；召回改「关键词+向量 RRF 混合」、失败自动回落关键词；schema v3（`brain_vector_index`）；63 条记忆全量回填 0 失败，跨语言检索/降级回退/端到端（写→补→搜→撤→清）全过；新命令 `vector-status/vector-drain/vector-test`；改动前备份 `backup-brain-plugin-20261014-234745.tar.gz`
16. （2026-10-15 凌晨）**向量层自动化加固**：用户问「是否正在起作用」→ 实测取证时发现三重效应会让自动向量化漏夜：① SF embed 尾延迟 20~60s 常见；② 短命进程（一次性 CLI/短会话）退出会把跑一半的 drain 线程杀掉；③ 旧版「锁被占就硬跳过」会连锁卡死全部回填。修复：`GET_LOCK` 改尽力而为（幂等继续）+ drainer 启动即首扫 + 新增 systemd `brain-vector-drain.timer`（每 3 分钟跑 `hermes brain vector-drain`，跑完即退，兜底所有无会话窗口）。无人值守实测：写入测试条目零干预，3 分钟内自动向量化（journal 证据 `embedded: 1`）并可从语义检索命中；测试数据已清理（64/64）。

---

*本文件由 Hermes agent 维护；如与实际配置有出入，以 `~/.hermes/brain.json`、`/root/agent-db/credentials.txt` 与运行中的容器为准。*
