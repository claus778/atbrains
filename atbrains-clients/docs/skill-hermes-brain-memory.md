---
name: hermes-brain-memory
description: "Use when brain memory or atbrains console needs work."
version: 1.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [memory, brain, redis, mariadb, maintenance, hermes]
    related_skills: [hermes-agent, hermes-memory-curation]
---

# brain 记忆提供者维护手册

brain = 本机自托管双存储记忆提供者（Redis 短期 + MariaDB 长期），2026-10-12 接入。

## When to Use

- brain 工具（brain_remember / brain_recall / brain_recent / brain_stats）在会话中不可用或召回为空；
- `hermes memory status` 显示 brain 不再 available/active，或它出现在 `plugins.disabled`；
- Hermes 大版本更新之后需要验证/恢复 brain；
- 需要修改 atbrains-memory 插件代码、配置或排查它的存储;
- 开发/修改 atbrains 监控控制台(插件形态:`hermes atbrains`;代码在 `/root/atbrains` git 仓库,见「交付物」节)。

## 结构（改代码前先读这里）

- 插件目录：`/root/.hermes/plugins/atbrains-memory/`
  - `__init__.py` 提供者（钩子 + 5 个工具）；`core.py` 存储引擎（双库读写/检索/召回）；`cli.py` = `hermes atbrains-memory` 命令
  - `vendor/` 内置纯 Python 的 redis-py + PyMySQL（**不要**改成 plugin.yaml `pip_dependencies`，见下）
  - `skills/brain/SKILL.md` 注册为 `brain:memory` 技能（provider 激活时可见）
- 配置：`~/.hermes/brain.json`（非密）；密码 `~/.hermes/.env`：`BRAIN_REDIS_PASSWORD` / `BRAIN_MARIADB_PASSWORD`
- 总览文档：`/root/agent_memory.md`（整套记忆系统配置全景，用户维护入口）
- 激活：`memory.provider: brain`（`hermes config set memory.provider atbrains-memory`）；出现在 `plugins.enabled` 里只是通用插件登记的装饰，激活只看 memory.provider
- 多设备：brain_memories / brain_turns / brain_sessions 带 `machine` 列标来源机（brain.json `machine`，缺省=主机名；服务器=server）；召回片段自动标注 [machine]；多设备库号规划（Redis db0-db4 预留 hermes 系；server=db0、x260-debian=db1（原 x260，2026-10-13 更名）、windows-laptop=db2、db3=新实例(10-13 交付)、db4 空位；非 hermes 从 db5 起）见 `references/namespace-separation.md`；新设备/新实例接入的提示词工单（可整段粘贴给目标机 Hermes 执行）见 `references/device-onboarding-prompt.md`
- 服务：Redis 容器 `:9079`、MariaDB 容器 `:57306`（2026-10-12 重建后两端口已绑 0.0.0.0；127.0.0.1 仍可用；主机 nftables 只黑名单 :22；腾讯云安全组已放行 57306/9079，2026-10-13 外部多点实测公网直连可达），库 `agent_memory`（表：brain_memories / brain_turns / brain_sessions / brain_archive / brain_meta；多 agent 分库台账见 `/root/agent-db/credentials.txt`）

## 体检（先跑这些定位）

```bash
hermes memory status            # provider 是否 available/active
hermes atbrains-memory doctor             # 依赖/凭据/连接/表结构
hermes atbrains-memory status             # 连通性 + 计数
```

端到端验证（改过 provider、迁移或重建容器后必做）：`hermes chat -q "只凭记忆回答：<只有库里知道的事实>"`——
输出应出现 `🧠 brain — recalled N memories`，且 0 次工具调用就能答对；随后核对 brain_turns 与 Redis 流各 +1
（确认每轮自动双写归档在生效）。

独立复现（绕过 Hermes，直接用运行时 Python）：

```bash
HERMES_DISABLE_LAZY_INSTALLS=1 /root/.hermes/tools/python-3.14.7+*/bin/python3 -I -c \
  'import sys; sys.path.insert(0,"/root/.hermes/plugins/atbrains-memory");
   from core import BrainStore, resolve_config;
   s=BrainStore(resolve_config("/root/.hermes")); print(s.ping())'
```

完整性审计（本机会话全集 `state.db` ↔ `brain_turns` 对账，可回填缺失轮次）：

```bash
python3 /root/agent-db/audit-turns.py          # 只审计（读 state.db + MariaDB）
python3 /root/agent-db/audit-turns.py --apply  # 审计并回填
```

每台机器在各自机器上跑；其它机（如 x260-debian）可带 `--machine x260-debian --since '大致上线时刻'` 等参数指向本机 state.db/插件目录。

记忆侧完整性审计（内置 `MEMORY.md`/`USER.md` ↔ `brain_memories` 对账，只读、秒级）：

```bash
python3 <skill_dir>/scripts/audit-memories.py [/path/to/.hermes]   # 默认 /root/.hermes
```

应输出「未镜像 0 条」。出现未镜像说明内置改了但没写进库 —— 或上次整理**只做了一侧**（见「记忆库整理」）。
这是用户报「历史遗漏」时应该先跑的那个（见 6b）。

## 已知故障与修复

1) **某次更新后 memory.provider 变空 / 插件出现在 `plugins.disabled`**
   症状：`hermes memory status` 不再显示 brain；日志出现 "Disabled plugin 'brain' ... dependency environment no longer builds"。
   原因：只要 plugin.yaml 声明了 `pip_dependencies`，PM 会把插件并入 uv workspace 重解析；本服务器 PyPI 腾讯镜像缺 PEP 700 `upload-time` 元数据（uv exclude-newer 隔离需要），解析必败 → PM 驱逐插件。
   修复：确认 plugin.yaml 没有 `pip_dependencies`（依赖必须保持 vendor 方式）；然后
   `hermes config set plugins.disabled '[]'` 或从 config.yaml 的 plugins.disabled 移除 brain，
   再 `hermes config set memory.provider atbrains-memory`。
   新设备报「找不到 atbrains-memory 插件」时：先复核插件目录与 vendor 完整性（Windows=%USERPROFILE%\.hermes\plugins\brain），再看 `hermes plugins list`、`hermes memory status` 定位；服务器压包见 /root/agent-db/dist/（最新 `atbrains-memory-20261013.tar.gz/.zip`）。详见 references/device-onboarding-prompt.md 步骤 1。

2) **新会话工具里没有 brain_remember / 召回为空**
   检查 provider：`hermes memory status`（available ✓）；若 unavailable 看 `hermes atbrains-memory doctor` 输出。
   常见：`.env` 里的 BRAIN_* 密码被覆盖/删除 → 重写（用 `hermes_cli.config.save_env_value`，别手写引号）。

3) **`import redis` / `import pymysql` 失败**
   vendor 目录损坏或缺失。修复：
   `pip download redis PyMySQL -d /tmp/w && python3 -m zipfile -e /tmp/w/redis-*.whl /root/.hermes/plugins/atbrains-memory/vendor/ && python3 -m zipfile -e /tmp/w/pymysql-*.whl /root/.hermes/plugins/atbrains-memory/vendor/`

4) **数据库/缓存容器挂了 / 需保数据重建**
   先试 `docker start redis mariadb`；凭据在 `~/.hermes/.env`（Redis 需 `--requirepass`；MariaDB 用户 `agent`）。
   保数据重建：先记下两侧计数（brain_memories/turns/sessions、Redis DBSIZE 与 XLEN brain:recent）→
   `docker stop mariadb redis`（优雅停，别 `docker rm -f`，让 AOF/InnoDB 落盘）→ `docker rm` → 原命令 `docker run`，
   务必挂回数据目录 `/root/mariadb` / `/root/redis`（2026-10-15 起绑定挂载; 旧命名卷已弃用、保留未删）→ 启动后核对计数 + 实连。
   ⚠️ 改端口绑定的坑：docker 的 `-p` 只转发“绑定的那个 IP”——把 `-p 57306:3306` 改成单 IP 绑定（如
   `-p 10.11.12.1:57306:3306`）后，127.0.0.1 也会连不上（brain.json 默认走 127.0.0.1）。要么同端口加两条 -p
   （`127.0.0.1` + 目标 IP），要么同步改 `~/.hermes/brain.json` 的 mariadb_host / redis_host。

5) **微信/网关侧还是旧记忆行为**
   提供者在 agent 会话初始化时加载：新开会话自动生效；要强制全体生效 `hermes gateway restart`（微信有 ~15 分钟 context_token 限制，重启通知可能发不出，属已知现象）。

6) **brain_turns 缺轮次（对话归档不完整）**
   成因一：提供者只在会话/进程初始化时读配置——改过 `brain.json`（库名、地址）后未重启的 CLI/网关仍按旧配置写，表不存在即写入失败，轮次静默丢失（日志特征 `brain: sync_turn 写入失败: (1146, "Table 'agent_memory.brain_turns' doesn't exist")`）。成因二：插件激活之前的历史轮次本就不归档。
   排查：`grep "sync_turn 写入失败" ~/.hermes/logs/agent.log` 找丢写窗口；用 `audit-turns.py` 对账。
   修复：`audit-turns.py --apply` 从 state.db 回填（含双方全文、自动校正 turn_count）。
   预防：配置变更后立即重启 CLI/网关，并跑一次审计。
   注意：brain_turns 的 id 断档不必然是丢数据——失败/回滚的批量插入会烧 auto_increment 号；完整性看行数对账，别看 id 连续。

6b) **别人报「mariadb 有历史遗漏」时，先分清是哪一侧**
   两侧含义完全不同，别一上来就回填对话：
   * **记忆侧（用户真正在意的）**：内置 `MEMORY.md`/`USER.md` 的条目没镜像进 brain_memories
     → 跑 `scripts/audit-memories.py` 对账，按「记忆库整理」修。
   * **对话侧**：`brain_turns` 缺轮次。**这不需要修** —— 用户已确认不同实例之间
     **只同步记忆、不同步对话记录**，所以不要跨机回填/推送轮次，也别因为别的机器
     轮次少就去补。
   量对话侧时的陷阱：`audit-turns.py` 默认 `--since` 是 brain 上线时刻，用默认值必然报
   「缺 0 轮」，把真实缺口藏起来；要量就把起点拉到最早。该工具保留给「本机自己的轮次
   因配置变更丢写」用（见 6）。
   * 假缺失（2026-10-13 实测）：回合中的「用户纠偏」（user correction）会被合并进同一条归档行，
     audit 逐条比对会把两个用户消息都判成缺失；这种回填无害（原文多存一份），也可忽略。

## 内置记忆与数据库的镜像关系

- 内置 MEMORY.md / USER.md 每轮固定注入，brain 是它的完整镜像（`on_memory_write` 钩子自动双写；replace/remove 按 `previous_content` 精确匹配表内旧行）。
- 回填/导入内置记忆时**逐字节保留原文**（按行 `§` 拆分；不要做空白规范化，否则镜像的 replace/remove 匹配不上旧行）；导入前按 active 内容去重，脚本保持幂等。
- 多 agent 命名空间分离已于 2026-10-12 执行：hermes→`agent_memory`（旧名 agent_memory 已删）；omp/dsh/artex→各自 `<name>_agent_memory`+同名用户+Redis db5-7（重排前曾为 db1-3）。交付物在 `/root/agent-db/`（credentials.txt 台账 / rebuild.sh 重建命令 / init.sql 空卷初始化 / verify-accounts.py 自检）。机制事实与执行记录见 `references/namespace-separation.md` —— 跨库 `RENAME TABLE` 是秒级无损的元数据操作。

## 记忆库整理（去重 / 清测试垃圾）

用户说「整理记忆库」时跑这个流程。核心：**停用要走 brain 工具，不要手写 SQL 改 active。**

1. 直连 MariaDB 取全貌（连接参数在 `brain.json`；密码只从 `.env` 读且不回显）：

```bash
export MYSQL_PWD=$(python3 -c "
for l in open('/root/.hermes/.env', encoding='utf-8', errors='replace'):
    if l.startswith('BRAIN_MARIADB_PASSWORD'):
        print(l.split('=',1)[1].strip().strip('\"').strip(chr(39))); break")
M="mysql -h127.0.0.1 -P57306 -uagent agent_memory --default-character-set=utf8mb4 -N -B"
$M -e "SELECT id,machine,kind,importance,active,LEFT(content,100) FROM brain_memories WHERE active=1 ORDER BY machine,id;"
```

2. 找重复：同一件事常存多条（迁移导入的 + 各机器镜像的 + 手工写的）。
   **合并必须「内置 + 库」两侧一起做** —— 只改库会让两边分叉：内置文件仍留旧重复行，
   每轮注入照样重复，而且镜像对账会冒出一批「未镜像」条目。正确顺序：
   a) 重复项来自内置记忆（`source='memory_write'` / `migration`）→ **先改内置**：
      用 `memory` 工具 replace/remove 把行并掉；镜像自动跟随
      （replace = 停用旧行 + 写一条新行；remove = 停用旧行）。
   b) 纯 brain 条目（手工写的 / 别的机器遗留）→ `atbrains_remember` 一条合并版 + `atbrains_forget` 掉旧的。
   c) 若此前已手工 `atbrains_remember` 写过合并条目，此时它会与 (a) 产生的镜像行重复 ——
      停用 `source='tool'` 那几条，保留 `source='memory_write'` 的镜像行。
   d) 收尾跑 `scripts/audit-memories.py`，必须报「未镜像 0 条」。
3. 清测试垃圾：`source='standalone-test'`、或内容含「验证 / 测试 / 接入 OK」的接入期条目——
   它们常被标成 `importance=9`，会污染召回排序。
4. 收尾核对：`atbrains_stats`（active 数下降 + `redis.hot_index` 应同步下降）+ `hermes atbrains-memory doctor`。

### 排序与浏览（2026-10-13 起口径：最新在前）

- 库内视图：`brain_memories_newest` / `brain_turns_newest` / `brain_sessions_newest` —— 直接 SELECT 即最新在前（实测带 WHERE 过滤也保持；外层自带 ORDER BY 时以外层为准）。
- 全库总览/快照：`python3 /root/agent-db/brain-overview.py [--all] [--machine X] [--out FILE]`（纯 stdlib + mysql CLI，不需要插件运行时）。
- 裸表没有固定显示顺序（InnoDB 按主键聚簇，裸看默认老在前）；手动查询用 `ORDER BY created_at/ts/last_active DESC`。
- **回填/修正 machine 的 UPDATE 必须带 `updated_at=updated_at`** —— 否则 `ON UPDATE CURRENT_TIMESTAMP` 会把老条目的 updated_at 顶成当下，扰动召回排序（memory 表的坑；turns/sessions 无此列）。
- 改完 machine 后跑 `/root/agent-db/refresh-hot-machine.py`（插件运行时 python 执行，先 `--dry-run`；加 `--redis-db N` 可校正其它 agent 的库，如 x260-debian 的 db1）：`brain:hot:<id>` 是写入时快照、`atbrains_recent(what=memories)` 直接读快照，不同步会残留旧标签/空标签。
- 「最新在前」只约束浏览/列表口径；召回（prefetch / search_memories）保持「重要度优先、同重要度最新在前」，别改。
- **2026-10-14 全表重排**：`brain_turns` 已按 ts 重建、连续重编号 1..N（id 即时间序，浏览/对账都顺）；旧编号仅保留在 `backup-brain_turns-20261014-pre-rebuild.sql`（库内备份表 `brain_turns_bak_20261014` 已于 2026-10-14 验证后删除）。再整理跑 `/root/agent-db/reorder-turns.sh`（v3：时间戳 dump + dump 恢复自检 + **显式 ROW_NUMBER 行号插入** + orig_id 漂移追踪 + 阻断式校验含 `next_id==MAX(id)+1`；不留库内 bak，回滚窗口=old_tmp）。⚠️ 教训：用大批量 `INSERT...SELECT` 重建表会触发 InnoDB auto_increment 按 2 的幂预留下一个值（实测 550 行 → 计数器停在 1024，真实 id 出现跳变）；凡重建/批量写入表必须显式指定 id 并校验 next_id = MAX(id)+1。注意：重编号后任何回填都会再次在尾部引入「旧时间行」（id 序≠ts 序），属正常语义、不是故障。
- **删除库内备份表前必须验证**：把随附 dump 恢复进 scratch 表与备份表逐行比对（行数 + 全列 JOIN + CRC32 聚合三项全一致才 DROP，比对时逐行确认备份表内容已全部存在于现表）；dump 文件本身保留在 `/root/agent-db/`。

### 两个必踩的连接坑

- **`.env` 里的值是带引号的。** `set -a; . .env` 会让密码变形（实测 84→82 字节），
  换来一个误导性的 `ERROR 1045 Access denied`——看着像凭据错，其实是读法错。
  永远用上面那段 Python 解析，然后走 `MYSQL_PWD`。
- **列名是 `active`（tinyint 0/1），不是 `deleted_at`。** 写错列名是 `ERROR 1054`；
  如果同时把 stderr `2>/dev/null` 掉，查询会**静默返回空**，看起来像「库里没数据」。

### 为什么停用必须走 brain 工具

`store.forget()` 除了 `UPDATE brain_memories SET active=0`，还会
`DEL brain:hot:<id>` + `ZREM brain:hot:index`。**直接写 SQL 会把 Redis 热点镜像留脏**，
已停用的条目仍从短期层被召回。

### 镜像会不会把删掉的条目重新灌回来？不会

镜像是 `on_memory_write` 钩子驱动的**事件型写入**（add/replace/remove 才触发），
不是周期性重扫 `MEMORY.md`。所以停用镜像条目是有效的，直到那条内置记忆再次被写。

### 跨机器边界（用户已确认的硬规则）

- 一个 agent 只写自己 `machine` 的行。**别的机器的测试垃圾报告给用户，不要代删。**
- Redis 短期层一库一 agent 各自独立，**永不跨库/跨机同步**；只有 MariaDB 长期层全机共享。
- **跨机只同步「记忆」（brain_memories），不同步「对话记录」（brain_turns）。**
  不要提议或执行跨机轮次回填；别的机器轮次少不是故障，不用处理。
- **skill 不入库、不同步（2026-10-13 用户确认）**：各机自动生成的 skill 本体留本地（自家 Curator 治理）；
  跨机可复用的写一条 brain 指针记忆（machine + skill 名 + 一句话用途），要本体就按需拷贝目录。
- `machine=''` 且 `source='migration'` 的是 machine 列上线前导入的存量，**回填前先问用户**。

## brain_archive 与时间核验（2026-10-14 整理）

**archive 只在「上下文压缩」那一刻写入**：会话达到压缩阈值（约上下文 85%，如 200k/1M token）时，
`on_pre_compress` 把压缩前消息逐条截 2000 字存一行（kind='precompress'）。它 **不是**「清理/归档老数据的地方」——
prune 按保留期直接 DELETE（turns 默认还全量保留），所以 brain_archive 常年近空是正常现象。
服务器历次真实压缩（查 `grep -E 'auto-compressing|Session split' ~/.hermes/logs/gateway.log`：6-26 / 9-14 / 9-23 / 10-01）
都发生在 brain 上线（10-12）之前，未记录；上线后唯一一次 = 10-13 晚 x260-debian 长会话（1 行 21KB）。
用户问「archive 为什么没数据」按此解释；「压缩过却没新行」才需查 hook/网络。

**「时间乱不乱」核验套路**（用户报时间问题先跑；全库口径=UTC、比北京时间 -8h、四表一致）：
- 逐行对账：Redis `brain:recent` 流 epoch（各机本地时钟）vs 库内 ts —— 差 0~2s 即一致，差 ±8h 才是真错；
  server 行用 state.db 全量对账（audit-turns.py 底层逻辑）。2026-10-14 核过 542 行：全部一致、无错误行。
- 各机时钟基准：服务器 NTP 精确（chrony，与外部 HTTP Date 对表 ±1s）；x260-debian 慢约 53 秒（其行比流
  「快」~52s 是机器时钟差、非库错）。跨机秒/分钟级差异先怀疑机器时钟。
- 「id 序 ≠ ts 序」倒挂 = 回填行（旧时间、晚插入），正常现象；嫌乱跑 reorder-turns.sh 重排。

## 表结构演进（v2，2026-10-13 已落地）

- 服务器 MariaDB **13.0.2**（`11.8.6` 只是客户端版本号）；已实测幂等能力：
  `ADD INDEX/UNIQUE KEY/CONSTRAINT/COLUMN IF NOT EXISTS`、STORED 生成列、CHECK（错值报 4025）、`VECTOR(n)+向量索引`。
- 结构版本记录在 `brain_meta.schema_version`（当前 **v2**）；插件 `ensure_schema()` 启动时自动幂等迁移
  （`_MIGRATIONS` + `GET_LOCK` 串行化），`hermes atbrains-memory doctor` 显示版本号。
- v2 内容：`brain_memories` 活跃行防重唯一键 `uniq_active_content`（生成列 MD5，撞 1062 时 `remember()`
  自动降级为「刷新既有行」并返回 `deduped:true`）、`idx_recall (active, importance, updated_at)`、
  `deactivated_at`（prune 计时用它）、CHECK(importance/active)、`content`→MEDIUMTEXT；
  `brain_turns.truncated`；`brain_archive.machine/platform`。
- 时区：**库内统一 UTC（+00:00）**（容器 default-time-zone 固定；2026-10-13 曾整体切至 +08:00，
  同日按用户要求回退为 UTC，数据已 -8h 还原）。看库/写脚本一律按 UTC（比北京时间慢 8 小时）；
  audit-turns.py 回填口径=UTC（各机旧副本同为 UTC 口径可继续用；新版另含 truncated 列写入）。
- prune 默认：`turns_keep_days=0`（对话不清理）、`sessions_keep_days=365`（无近轮会话才删）；
  清理由**插件内维护循环**执行（见「维护与自动化」），手动仍可 `python3 /root/agent-db/prune.sh` 或 `hermes atbrains-memory prune`。
- 检索时间窗：`search_turns_days`（默认 120 天）只影响召回/检索，不动归档数据。
- ⚠️ `rebuild.sh`/`credentials.txt`/`init.sql` 由 `/root/agent-db/generate-artifacts.py` 生成（模板内嵌该脚本）
  ——手改这些文件必须同步改生成器模板，否则下次生成会被覆盖（时区/注释等已进模板）。
- 备份：升级前全量 `backup-agent_memory-20261013-pre-v2.sql`；插件旧版在 `atbrains-memory-backup-20261013/`
  （含 audit/rebuild 的 .orig）。分发包已更新 `dist/atbrains-memory-20261013.tar.gz/.zip`（其他机器要用新版时换包）。

## 向量层（语义召回，2026-10-14 深夜接入）

- 管线：写入 → `_vec_wake`（另有 60s 扫描；线程启动即先扫一遍）→ drainer 线程（`GET_LOCK('brain_vector_drain')` 尽力而为：抢不到锁也继续跑，靠幂等兜底）
  → SiliconFlow `Qwen/Qwen3-Embedding-4B`（2560 维）→ Qdrant 本机容器 `qdrant`（127.0.0.1:6333）集合 `brain_memories`；
  召回 = 关键词 + 向量 topk12 → RRF（k=60）；任何失败自动回落纯关键词（日志特征 `回落纯关键词`）。
- **无人值守兜底 = 进程内巡检（2026-10-17 起，零 systemd）**：任何加载了插件的**长命进程**
  （网关最典型）每 60 秒扫一次待向量化行；短命进程尽力而为后 **0.3 秒内退出**，遗留欠账由下一个长命进程接手。
  背景：SF embed 尾延迟常见 20-60s，短命进程（一次性 CLI/短会话）会在跑一半时退出把线程杀掉。
  实测（2026-10-17）：停掉并最终删除 `brain-vector-drain.timer` 后，短命进程写入的记忆
  **100 秒内**被网关巡检线程自动补上向量；而定时器在职时上限是 3 分钟 —— 内建机制反而更快。
  ⇒ **不要再给这套系统加 systemd 定时器**（用户明确要求不依赖系统级服务）。
- **数据库清理**同样进程内：`brain-maintenance` 线程每 `maintenance_interval_min`(60) 检查
  `brain_meta.last_prune`，距上次超过 `prune_every_days`(7) 天才执行，靠 `GET_LOCK('brain_prune')` 多进程互斥；
  开关 `maintenance_enabled`。该线程在 `ensure_schema()` 成功后惰性启动、幂等（只有一个实例）。
- **脚本坑**：直接调插件 `_exec(sql)` 时，SQL 里裸写 `%`（如 `LIKE 'x%'`）会触发 pymysql 的 “%”-格式化错误——改传参（`LIKE %s`）或用 mysql CLI 查。
- **key 规则（用户硬要求）**：不落 Hermes 配置/记忆；唯一存放点 = qdrant 自己的配置文件
  `/root/qdrant/qdrant-config/production.yaml`（`service.api_key`，只读挂载进容器 /qdrant/config/production.yaml；embed 与 qdrant 同串）。
  插件直接读该文件（缓存 5 分钟；401 自动失效重读）；轮换 key = 改该文件 + `docker restart qdrant`。
  （历史：首版曾用 `QDRANT__SERVICE__API_KEY` 环境变量传 key，已按用户要求改为配置文件式，容器 env 中已无 key。）
- 调参（brain.json 非密）：`vector_enabled=0` 一键退回纯关键词；模型/维度/阈值/批量等默认值在 core.py DEFAULT_CONFIG。
  改模型/维度后 `vector-drain --force`；集合维度不符时 ensure 会明确报错（换集合名或清库重建）。
- 运维/排障：`vector-status`（开关/模型/key 来源/缺失/失败/过期计数）、`vector-drain`（失败行 30 分钟退避重试）、
  `vector-test <q>`（三方对比）；查询向量缓存 Redis `brain:qemb:*`（TTL 1h）；schema v3 表 `brain_vector_index`；
  软删记忆的向量由下一次 drain 顺带清理。
- 实测基准（2026-10-14）：63 条回填 0 失败；embed 单条 0.3~22s 波动（服务端排队），查询侧 20s 超时即降级；
  改动前备份 `backup-atbrains-memory-20261014-234745.tar.gz`（插件目录 + brain.json）。
- 隔离度演练（用户问「向量层会不会拖垮整个记忆」时跑）：`scripts/vector-fallback-test.py` —— 把 store 指向
  死 qdrant/死 embed/坏 key 路径，各 case 应打印 `回落纯关键词` 且返回关键词结果、不抛异常（2026-10-15 实测
  4/4 通过，0.0~0.1s 快速失败；结论：向量层故障只降级语义召回，不阻断记忆读写）。
- 原生替代路径（用户问过「为什么不用 MariaDB 原生查询」，评估过、未采用）：13.0.2 真表支持 `VECTOR(2560)` + `VECTOR INDEX` + `VEC_DISTANCE_COSINE`（2026-10-15 探针表实测：建列/建索引/插入/语义排序全部 OK；小表上优化器走全扫属正常）。⚠️ **临时表不能建向量索引**：`ERROR 1796 Cannot create VECTOR index on temporary InnoDB table`——验证这类能力必须用真表，用完即删。当前 Qdrant 是用户先自行部署后接入的选型；若未来决定迁回原生：改插件向量层（HTTP→SQL）、回填向量、下架 qdrant 容器，先出方案再动手。
- 其它机器：分发包 dist/ 仍是旧版；跑新代码也没有本地 qdrant 容器 → 读不到 key → 向量层自动停用（不影响关键词）。
  给 x260-debian / r9000p 开语义召回需先解决 Qdrant 可达性（安全组 6333 + key）或本地部署——后续单独决策。

## 检索质量层（2026-10-15 加装，schema v4）

四件东西，全部已上线并验收（`/root/atbrains/scripts/verify-refactor.py` 29/29）：

### 记忆分层 + 时间衰减

- `brain_memories.memory_type` ∈ {`profile`（builtin-memory，永衰减）、`knowledge`（fact/decision，永不衰减）、
  `episodic`（note/todo 等，半衰期 45 天）}；映射函数 `_memory_type(kind, explicit)`。
- 衰减在 RRF 打分后、排序前乘 `0.5 ** (age/半衰期)`，**只作用于 `decay_types` 里的层**；
  年龄取 `updated_at` → 缺失回落 `created_at` → 仍缺失则**不改分**（不丢结果）。
- 调参：`decay_enabled` / `decay_half_life_days` / `decay_types`（brain.json）。
- ⚠️ **衰减使 `updated_at` 变成关键信号**——任何批量 UPDATE 必须显式带 `updated_at = updated_at`，
  否则 `ON UPDATE CURRENT_TIMESTAMP` 会把全表年龄冲成当下。（本层落地时真踩过：迁移回填把 103 行冲平，
  已按 `GREATEST(created_at, COALESCE(deactivated_at, created_at))` 还原成 62 个不同值。）

### 记忆取代（Supersede）

- `superseded_by` / `superseded_at`（可空，**不是停用**——`active` 仍为 1，取代可无损回滚）。
- `detect_supersedes(dry_run=True, threshold=0.90)`：同 `memory_type` 内两两比**已存在的 Qdrant 向量**
  （不重新 embed），相似度 ≥ 阈值的一对里 **`created_at` 更新者胜**，旧条打标记并计一条 marked（保证无环）。
- 检索侧四处过滤 `superseded_by IS NULL`（关键词 / 向量 / hybrid / 取代扫描本身）——已取代的记忆不再注入。
- CLI：`hermes atbrains-memory supersede-detect`（**默认预演**）/ `--apply` / `hermes atbrains-memory unsupersede <id>`。
- **踩过的坑**：首版比较方向写反（较新者被判为「被取代」）。现象是 dry-run 输出里
  `id 大/时间新的那条` 出现在 `id` 字段。验收必须核对方向，不能只看「检出 N 组」。
- 阈值口味：0.90 只抓近似重复（实测库里 2 组）；0.80 会抓到 8 组（开始出现同主题不同版本的条目）。

### Rerank（默认关闭）

- `rerank_enabled=0` 默认关；开了以后在 RRF 之后调硅基流动 `BAAI/bge-reranker-v2-m3`
  （实测 216ms，**`bce-reranker-base_v1` 已被上游禁用**——Model disabled，别用）。
- key 复用向量层 embed key（`_vector_secrets()['embed']`）；失败只 logger.warning + 保持 RRF 顺序，**绝不抛**。
- 效果实测：同一批候选，RRF 分不开而 rerank 给出 `0.95/0.92` vs `0.03/0.0002` 的悬崖——
  配 `rerank_min_score`（如 0.3）可真正切掉噪声。

### Memory Service（/v1）与客户端远程模式

- 服务端在 **atbrains 插件**（`service.py`），与监控控制台**共用 8787 端口**；
  `/v1/*` 走 Bearer 令牌（`service-tokens.json`，600，不入 git），`/api/*` 仍走控制台 token，两者互不通用。
- **服务端用令牌反查 machine**（客户端在 body 里塞 machine 无效）——这是防伪造的设计要点。
- 客户端（atbrains-memory 插件）：`remote_enabled` 等 6 个配置**默认全关**，开了之后 `prefetch_block` / `remember` /
  `forget` / `record_turn`(需 `remote_turn_sync=1`) 走 HTTP；**任何失败自动回落本地直连**
  （实测死地址 33ms 回落），令牌放 `.env` 的 `BRAIN_REMOTE_TOKEN`（不进 brain.json）。
- `hermes atbrains-memory remote-check` 自检；`hermes atbrains token-list|token-add|token-rm|service` 管令牌。
- 迁移策略：**客户端先切、服务端自己留本地**（服务端本地直连本来就是最短路径）。

### Redis key 前缀（默认关闭）

- `redis_key_prefix=""` 时 key 与历史**逐字节一致**（硬验收项）；设 `brain:server` 即启用前缀隔离。
- 一次性迁移：`python3 /root/agent-db/migrate-redis-prefix.py --prefix brain:server`（默认预演，
  `--apply` 才 RENAME；目标键已存在会拒绝覆盖并计冲突）。实测 db0 88 键、0 冲突。
- 切前缀前**先给该实例配 `redis_key_prefix`**，否则运行时会找新键名（只有短期缓存受影响，
  MariaDB 长期层无损，缺的缓存自然回填）。

## 交付物：`/root/atbrains` 资料库 + `atbrains` 插件(监控控制台)

brain 有一套**可打包/可交付/可归档**的整理件：`/root/atbrains/`。它的 `README.md` 是总文档
（架构图 / 目录树 / 运维命令 / 故障速查）—— 要向用户解释这套系统时先读它，别再写一份。

### 结构:代码的家在仓库,安装副本在插件目录(2026-10-15 用户定稿)

```
/root/atbrains/plugin/          ← 代码真身(git 管理;改这里)     git log 看历史
/root/atbrains/install.sh       ← 安装脚本
~/.hermes/plugins/atbrains/     ← 安装副本(Hermes 实际加载;勿直接改)
```

- **改动流程:改仓库 → `./install.sh`**(rsync 同步;保留安装副本 `console/.token`,排除 `__pycache__`)。
  `./install.sh check` 只看差异;`--restart-console` 顺带重启控制台。
- ⚠️ **委托 omp 时 `--cwd` 必须指向仓库 `/root/atbrains`，而不是安装副本** —— 否则
  改完的就是副本，仓库版还是旧的，得手工拷回去再 commit（2026-10-15 真踩过：
  `/v1` 服务、令牌体系那批改动全落在了安装副本，事后靠 `diff -rq` 才对齐）。
  定式：`omp -p --cwd /root/atbrains "<工单>"` → `./install.sh` → 跑验收。
- ⚠️ **工单里要写明「按 PID 杀进程」** —— omp 会用 `pkill -f <路径模式>` 清理测试进程，
  而那个模式会命中**宿主在跑的同一个 server.py**（2026-10-15 真把 8787 上的控制台误杀，
  停机约 1 分钟）。凡涉及起停进程/释放端口的工单，都写明：记录 PID 后精确 `kill`，禁止 `pkill -f`。
- 改了 `.py` → 重启控制台进程才生效;只改 `console/index.html` → 刷新页面即生效(每次请求都从磁盘读)。
- `.gitignore` 排除:`__pycache__/`、`console/.token`、`*.log`;git 身份 = claus778;
  习惯:动手大改前先提一个基线快照 commit,发布后打 `vX.Y.Z` tag(回看/回退都靠它)。
- 用户明确要求**不用软链接方案**(「在文件夹内写好代码,然后安装到 hermes 内」)—— 别改回去。

### 形态:**Hermes 插件,不是系统服务**(用户 2026-10-15 明确要求)

- **三面共用一份数据层 `probe.py`** —— CLI、agent 工具、网页都调它,
  避免「网页 65 条 / CLI 64 条」这类漂移。改采集逻辑只改 probe.py。
- **命令行**:`hermes atbrains status|serve|stop|open|drain|doctor|memories|turns|sessions|search|remember|forget|restore`
- **agent 工具**:`atbrains_status` / `atbrains_control`(action=drain/doctor/remember/forget/**restore**/prune/recent)
  —— 微信里直接问「记忆系统健康吗」即可,不必开网页。
- **网页控制台(v1.1.0,2026-10-15 重做 UI)**:`hermes atbrains serve --background` 起、`stop` 停
  (pidfile `/tmp/atbrains-console.pid`)。口令在 `~/.hermes/plugins/atbrains/console/.token`(600,首启自动生成)——
  **不回显、不进文档、不进聊天**(只在安装副本里;仓库没有,已 gitignore)。默认用法是 SSH 隧道:
  `ssh -p 7622 -L 8787:127.0.0.1:8787 root@<服务器>` → `http://127.0.0.1:8787/`。
  面板 6 个:记忆 / 对话归档 / 会话 / 动态(Redis 实时) / 检索 / 操作;
  记忆与对话可点开**详情弹窗**、行内**停用·恢复**、分页;写操作带确认;自动刷新 5/10/30s 可调(页面隐藏暂停)。
  后端接口 `/api/memory|turn|sessions|restore` 等见 plugin/README.md。
- **`restore` 与 `forget` 对偶** —— 改 MariaDB `active` + 维护 Redis 热点镜像(`_hot_put` 规则);
  同内容已有活跃条目时拒绝(库有 `uniq_active_content` 唯一键);向量由定时器 ≤3 分钟补回。
- **写插件时的两个硬约束**:
  1. `plugin.yaml` **不声明 `pip_dependencies`** —— 只依赖 stdlib + 兄弟插件 brain 的 vendor 库。
     一旦声明,PM 会把插件并入 uv workspace 重解析,国内镜像缺 PEP 700 元数据 → 解析失败 → 插件被自动禁用。
  2. **不要给它写 systemd 单元** —— 用户要的就是「插件形态、按需启停」;进程生命周期跟着
     `hermes atbrains serve|stop` 走,不常驻、不注册开机自启。
- **改动/验收时的大小坑**:
  1. **库行的时间列是 `datetime` 对象,不能当字符串切片** —— `(row['ts'] or '')[:16]` 抛
     `TypeError: 'datetime.datetime' object is not subscriptable`,整条命令崩溃;格式化统一
     `str(v)[:16]`(cli.py 的 `_ts()` 助手),任何读库脚本同理。
  2. **验收要完整跑命令并核对 exit code,不要截断输出** —— 用管道只取前/后几行会把 Traceback
     藏掉,崩溃会混过整轮验收(turns/search 的崩溃就是这样漏过一轮「回归」的)。
  3. **probe 找 atbrains-memory 插件是三级探测**(`ATBRAINS_BRAIN_PLUGIN` → 与插件并排的 `../brain` →
     `HERMES_HOME/plugins/atbrains-memory`):仓库直跑(plugin/ 不在 plugins/ 下)时靠第三级命中。
  4. **timer 的「下次运行」**:本机 systemd `NextElapseUSecRealtime` 取空;probe 改从
     `systemctl list-timers` 正则解析(`timer.nextRun`),别用 `systemctl show` 那个属性。
  5. **无头 Chromium 缺 emoji 字体**(✅/⚠️ 渲染成方块)—— 关键状态文字别依赖 emoji,用颜色类名;
     截图验收时留意。
  6. **测「停用/恢复」别拿库里现成条目当靶子** —— 停用行可能是刻意清理的残留(测试垃圾/去重);
     自建一条测试记忆走完整「建→停→恢复→停」并收尾保持停用;「已是活跃」幂等分支拿任意活跃 ID 测即可(不写库)。
- **依赖 atbrains-memory 插件**:通过其 `core.BrainStore` 读写,复用凭据解析与重试;
  brain 缺失时抛 `ProbeError` 明确报错,不静默失败。
- **启用/排障**:`hermes plugins enable atbrains`(工具集在**新会话**生效;
  `hermes tools list | grep atbrains` 应显示 `✓ enabled`)、`hermes plugins info atbrains`(版本对照 plugin/plugin.yaml)。
  gateway(微信侧)让插件改动生效:调 control socket 的 `reload-plugins`,实测命令:
  `HERMES_HOME=/root/.hermes <运行时 python> -I -c "import sys;sys.path.insert(0,'/usr/local/lib/hermes-agent');from pathlib import Path;from gateway.control_socket import reload_gateway_plugins;print(reload_gateway_plugins(Path('/root/.hermes')))"`
  (返回 `reloaded: True`;handlers 即时生效,工具在**下一会话**生效)。

### 资料库/交付件(与代码同仓)

- **docs/scripts/setup 的重新生成**:`python3 tools/package.py`(拷贝 + 自动脱敏 + 复检;
  只写 docs/scripts/setup 清单,**不碰** plugin/、README、install.sh;旧位置 `/root/atbrains-package.py` 已迁入)。
  改内容改**生成器**,不要只手改副本,否则下次重跑冲掉手工修改。
- **脱敏约定**:真实密钥仍只在 `.env` / redis 容器启动参数 / `qdrant production.yaml`;
  `setup/SECRETS.md` 是只写路径的索引,并附「外发前一键自检」。改动或重新生成后跑一次自检再交付。
  ⚠️ 容器 inspect 的 env 里就有明文 Redis/MariaDB 口令 —— 打包脚本必须做值级替换,别只靠肉眼。
- **验证本地 web UI 真的能用**(不只是 curl 200):浏览器工具打开
  `http://127.0.0.1:8787/?token=<.token 里的值>`(免输入直连;token 别进聊天/输出)→ 读 `#conn` 确认「已连接」→
  各面板点一遍(`window.confirm=()=>true` 绕弹窗;可用 `Page.addScriptToEvaluateOnNewDocument` 挂
  `window.__errs` 错误收集器查 JS 错误)→ `capture_screenshot()` 存 `web/preview.png` / `web/preview-recent.png`。
  截图再过一遍 **vision 质检**(能发现 DOM 检查看不见的:emoji 缺字体成方块、数值被硬截断、时间格式不一),
  但 vision 报的「排版溢出」先用 DOM 度量复核(`getBoundingClientRect`/`scrollWidth`)再改 ——
  换行高的表格行会被误判成溢出,未经核实不要动布局。
  agent 工具同理:新会话里**实调一次** `atbrains_status` 才算真验证(`hermes tools list` 只证明配置到位)。

## 多 agent 接入（MCP 端点，2026-10-24 上线）

brain 是 Hermes 专属 provider；omp / dsh / Codex 不能加载它。对外通道 = atbrains 控制台 8787 上的
`POST /mcp`（标准 MCP JSON-RPC 2.0，`Authorization: Bearer <service-tokens.json 令牌>`，
令牌反查 agent 名）。

- 代码：`plugin/brain_mcp.py`（`BrainMCP`）+ `plugin/console/server.py` 的 `_mcp()` 路由；
  令牌沿用 `service.py` 的 `service-tokens.json`（与 `/v1` 共用）。
- **共享而非隔离**：所有 agent 写 `agent_memory`，以 `machine` 列标注来源（omp/dsh/Hermes
  互相可 recall）。`omp_agent_memory` / `dsh_agent_memory` / `artex_agent_memory` 三个空库保留，
  作将来完全隔离的备选，默认不用。
- 工具 10 个，与插件同名同参。omp 配置 `~/.omp/agent/mcp.json`；dsh 在
  `$DSH_HOME/cordis.patch.yml` 加 `@deepseek-ai/dsh-mcp-client` 行。详见 `docs/multi-agent.md`。
- 自检：`python3 /root/agent-db/mcp_client_test.py --url http://127.0.0.1:8787/mcp --agent omp --write-test`。
- ⚠️ `install.sh` 的 `rsync --delete` 必须保留 `--exclude='service-tokens.json'`，否则每次安装都会
  删掉运行时令牌表，所有 agent 的 MCP 令牌失效（本次已修）。
- ⚠️ omp 是 Bun 包，`cli.js` 22MB，Hermes 生命周期守卫拒绝从网关内执行（引用脚本超 1MiB 即
  fail-closed）；验收用 SDK 小脚本，不要试图在网关会话里跑 `omp` 二进制。
- ⚠️ 召回排序：RRF(k=60) 会让关键词第 1 名与向量第 1 名同分，同分按 importance 排，刚写入的
  低重要度记忆会被挤出 top-k。MCP 的 recall 增加了整句精确包含直查并提前（不改 Hermes 侧排序）。

## 不要做

- 不要在 plugin.yaml 加 `pip_dependencies` / “顺手让 PM 管依赖”(教训:会触发 uv 镜像解析失败 + 自动驱逐)。
- 不要给 `atbrains` 控制台写 systemd 服务 —— 用户要插件形态(按需 `serve`/`stop`)。
- 不要把 `~/.hermes/plugins/atbrains` 改回软链接、也不要在安装副本里直接改代码 —— 一律「仓库 → install.sh」。
- 不要改 `/usr/local/lib/hermes-agent` 源码。
- 不要把密码写进 brain.json 或聊天记录;只进 `.env`。
- 不要把真实 token/密钥写进 `/root/atbrains/` 的任何文件或 git 提交(token 只在安装副本,已 gitignore)。
