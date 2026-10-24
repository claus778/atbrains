# atbrains · 记忆系统资料库 + 代码仓库

> 本目录既是 **brain 双存储记忆系统**(Redis 短期 + MariaDB 长期 + Qdrant 向量)
> 的**资料库**(搭建材料 / 运维脚本 / 文档),也是它的**监控控制台代码仓库**
> (`plugin/`,git 管理,用 `install.sh` 安装到 Hermes)。
>
> 机器:`server`(腾讯云 Debian 13,2C4G)· 最近更新:2026-10-15(控制台 v1.1.0)

---

## 一、系统长什么样

```
                    ┌──────────────────────────────────────────┐
   Hermes 会话 ─────▶│  atbrains-memory 插件  ~/.hermes/plugins/atbrains-memory/     │
   (agent)           │  __init__.py  提供者钩子 + 5 个工具        │
                    │  core.py      存储引擎(读写/检索/召回)   │
                    │  cli.py       hermes atbrains-memory 命令            │
                    └───────┬───────────────┬──────────────┬────┘
                            │               │              │
              短期层 ◀──────┘      长期层 ◀──┘      向量层 ◀─┘
        ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
        │ Redis   :9079    │  │ MariaDB :57306   │  │ Qdrant  :6333    │
        │ db0(本机)        │  │ hermes_agent_    │  │ collection       │
        │ · 最近对话流      │  │ memory           │  │ brain_memories   │
        │ · 会话状态        │  │ · brain_memories │  │ · 2560 维向量     │
        │ · 热点镜像        │  │ · brain_turns    │  │ · 余弦距离        │
        │ · 检索缓存        │  │ · brain_sessions │  │ · 点ID=记忆ID     │
        │ TTL 自动过期      │  │ · brain_archive  │  │                  │
        └──────────────────┘  └──────────────────┘  └──────────────────┘
                                                        ▲
              嵌入模型: 硅基流动 Qwen/Qwen3-Embedding-4B ┘
              自动化:  插件内巡检(零 systemd)· 长命进程每 60 秒补向量
```

**分层语义**

| 层 | 存什么 | 生命周期 | 跨机共享 |
|---|---|---|---|
| Redis(短期) | 最近对话流、会话状态、热点镜像、检索缓存 | TTL 过期,不用管 | ❌ 一库一 agent,各自独立 |
| MariaDB(长期) | 记忆 + 全部对话归档 + 会话索引 | 永久 | ✅ **只有这层全机共享** |
| Qdrant(向量) | 记忆的 2560 维语义向量 | 随记忆同步 | 服务于语义检索 |

> **已确认的架构硬规则**:一个 agent 只写自己 `machine` 的行;短期层永不跨库/跨机同步;
> 补录/对账/回填只针对 MariaDB 且只写自己 machine 的行。
> **跨机只需要同步「记忆」,不需要同步「对话记录」。**

---

## 二、这个仓库怎么用(代码 → 安装)

```
/root/atbrains/plugin/    ← 代码的家(git 管理,改这里)
/root/atbrains/install.sh ← 安装脚本
```

```bash
./install.sh                    # 安装/更新到 ~/.hermes/plugins/atbrains
./install.sh check              # 只看仓库 ↔ 安装副本的差异(不写入)
./install.sh --restart-console  # 安装后顺便重启网页控制台(如果在跑)
```

- Hermes 只能从 `~/.hermes/plugins/atbrains` 加载插件,那里是**安装副本**;
  **改动一律先改仓库,再 install** —— 直接改安装副本会被下次同步覆盖。
- 安装副本的 `console/.token`(控制台口令)是运行时状态:**同步时保留、不入库**(已 gitignore)。
- 版本:`git log` 看历史;当前版本在 `plugin/plugin.yaml`(v1.1.0)。
- 改了 `.py` 后需重启控制台进程(`hermes atbrains stop && hermes atbrains serve --background`);
  只改 `console/index.html` 则刷新页面即生效。

---

## 三、目录结构

```
/root/atbrains/
├── README.md                        ← 你正在看的这份
├── install.sh                       ★ 安装脚本(仓库 → Hermes 插件目录)
├── .gitignore                       忽略 __pycache__ / console/.token / *.log
├── plugin/                          ★ 控制台代码——真身在这里(git 管理)
│   ├── plugin.yaml                  清单(刻意不声明 pip_dependencies)
│   ├── __init__.py                  register(ctx):2 个工具 + `hermes atbrains` 命令
│   ├── probe.py                     ★ 唯一数据层(CLI/工具/网页共用,不漂移)
│   ├── cli.py / tools.py            CLI 与 agent 工具实现
│   ├── console/                     网页控制台(server.py + index.html;.token 运行时生成)
│   └── README.md
├── tools/
│   └── package.py                   打包/脱敏生成器(刷新 docs/scripts/setup 的拷贝)
├── docs/                            ← 原始文档与技能手册
│   ├── database-schema.md           ★ 记忆准则 + 表/列字典(schema v9,权威)
│   ├── protocol-v1.md               /v1 跨进程契约(客户端必须遵守)
│   ├── agent_memory.md              整套记忆系统配置全景(用户维护入口)
│   ├── plugin-README.md             atbrains-memory 插件自带说明
│   ├── skill-hermes-brain-memory.md 维护手册(故障/修复/整理流程)
│   ├── skill-brain-memory.md        日常使用指南
│   └── brain-overview.txt           库内容快照
├── setup/                           ← 从零重建所需的一切
│   ├── README.md                    重建步骤(按顺序)
│   ├── SECRETS.md                   ★ 机密存放位置(只写路径,不写值)
│   ├── migration-20261012.sql       分库迁移(改名 + 多 agent 分库)
│   ├── init.sql / rebuild.sh / verify-accounts.py
│   ├── credentials-LEDGER-scrubbed.txt  台账(★已脱敏)
│   └── systemd/                     向量回填的 systemd 单元
├── scripts/                         ← 运维脚本
│   ├── audit-turns.py               对话归档完整性审计/回填
│   ├── audit-memories.py            内置记忆 ↔ brain 镜像对账
│   ├── brain-overview.py            库内容导出
│   ├── prune.sh                     保留期清理
│   └── refresh-hot-machine.py       刷新机器标记
└── web/                             ← 控制台预览图与说明
    ├── preview.png                  控制台首页(记忆页)
    └── preview-recent.png           动态页(Redis 短期层实时视图)
```

注:`docs/scripts/setup` 里的文件由 `tools/package.py` 从活系统**复制 + 脱敏**生成
(改内容改生成器,别只手改副本,否则下次生成会被覆盖)。

---

## 四、监控控制台(Hermes 插件形态)

**形态**:**Hermes 插件** `atbrains`(已 enabled),**不是系统服务** —— 没有 systemd 单元。
控制台由 `hermes atbrains serve` 按需启动、`stop` 停止。

**三个使用面**(共用同一份数据层 `probe.py`,结果不会漂移):

| 面 | 用法 |
|---|---|
| **命令行** | `hermes atbrains status` —— 一屏总览(最常用) |
| **Agent 工具** | `atbrains_status` / `atbrains_control` —— 微信里直接问"记忆系统健康吗" |
| **网页** | systemd 单元 `atbrains-console.service` 常驻(enable + Restart=always) | `systemctl restart atbrains-console` 重启用 |

```bash
hermes atbrains status          # 监控
hermes atbrains serve --background
hermes atbrains open            # 地址 + 口令(SSH 隧道用)
hermes atbrains stop

# v1.1.0 新增:
hermes atbrains sessions [--machine M]   # 会话索引
hermes atbrains restore <ID>             # 恢复一条被停用的记忆(与 forget 对偶)
```

### 怎么打开网页

**方式 A:SSH 隧道(推荐,不用动安全组)**
```bash
hermes atbrains serve --background
ssh -p 7622 -L 8787:127.0.0.1:8787 root@111.230.71.79
# 浏览器 → http://127.0.0.1:8787/
# 口令 → cat ~/.hermes/plugins/atbrains/console/.token
# 免输入 → http://127.0.0.1:8787/?token=<口令>   (打开即登录,登录后参数自动抹掉)
```

**方式 B:腾讯云安全组放行 8787**,直接访问 `http://111.230.71.79:8787/`
（⚠️ 暴露到公网,务必先确认 token 足够强;不放心就继续用方式 A）

### 能做什么(v1.1 面板)

| 面板 | 内容 |
|---|---|
| **卡片总览** | 三层计数/状态/容器健康;向量覆盖率(进度条 + 待回填数);定时器上次/下次运行与退出码 |
| **记忆** | 内容/类型/状态过滤 + 分页;点击行看全文;行内停用(软删除)与**恢复**;详情弹窗含时间戳与一键复制 |
| **对话归档** | 来源机/关键词过滤 + 分页 + 按会话精查;详情弹窗看完整问答(含截断标记) |
| **会话** | 会话索引(轮次/开始/最近活动),一键跳到该会话的对话轮次 |
| **动态** | Redis 短期层实时视图:热点记忆 + 最近对话流 |
| **检索** | **混合检索**:向量(2560 维语义)+ 关键词融合,同时命中记忆与历史对话 |
| **操作** | 回填向量 / 体检 / 新增记忆 / 按 ID 查看·停用·恢复 / 归档清理预演(不写库) |

自动刷新:跟随右上角设置(5s/10s/30s/关,默认 10s),页面不可见时暂停;
对话页可见时每 30 秒自动补刷。口令可折叠为「✓ 已认证」;写操作都有确认或回显。

### 后端接口(想自己接脚本时用)

```
GET  /api/overview                 三层总览(容器/计数/向量/定时器/版本)
GET  /api/memories?limit=&offset=&active=1|0|all&q=&kind=&machine=&agent=
GET  /api/memory?id=               单条记忆全量(详情弹窗用)
GET  /api/turns?limit=&offset=&machine=&session_id=&q=
GET  /api/turn?id=                 单轮对话全量(详情弹窗用)
GET  /api/sessions?limit=&offset=&machine=&q=
GET  /api/search?q=&limit=         混合检索
GET  /api/recent?limit=            Redis 热点 + 最近对话
POST /api/remember   {content,kind,importance,tags}
POST /api/forget     {id}          软删除
POST /api/restore    {id}          恢复(失败返回 {ok:false,error})
POST /api/drain                    触发向量回填
POST /api/doctor                   系统体检
POST /api/prune      {dry_run:true}
```

> 设计原则:**凭据只在服务端解析,绝不下发浏览器**;所有 `/api/*` 都要 token。

### MCP 端点(非 Hermes agent 接入,v1.2 起)

`POST /mcp` 是标准 MCP(JSON-RPC 2.0)端点,与 `/v1` 共用 8787 端口,但用
`Authorization: Bearer <service-tokens.json 令牌>` 鉴权,令牌决定 agent 身份。

```bash
hermes atbrains token-add omp      # 生成/轮换令牌(明文只显示一次)
hermes atbrains token-list         # 只看掩码
python3 /root/agent-db/mcp_client_test.py --agent omp --write-test   # 端到端自检
```

omp 的 `~/.omp/agent/mcp.json`、dsh 的 `cordis.patch.yml` 接法见
[`docs/multi-agent.md`](docs/multi-agent.md)。要点:**所有 agent 共用
`agent_memory` 长期库、同一张 `brain_memories` 表**,`agent` 列标写入方
(hermes/omp/dsh/…)、`machine` 列标主机;`atbrains_recall` 默认跨 agent 召回,
传 `agent=` 可只看某一方。

---

## 五、检索质量与 Memory Service(2026-10-15 加装)

### 记忆分层(schema v4)

每条记忆带 `memory_type`,决定它**是否随时间衰减**:

| 层 | 来源 kind | 衰减 | 说明 |
|---|---|---|---|
| `profile` | builtin-memory | ❌ 永不 | 用户画像/偏好,长期稳定 |
| `knowledge` | fact / decision | ❌ 永不 | 提炼出的事实与决策 |
| `episodic` | note / todo 等 | ✅ 半衰期 45 天 | 事件性上下文,自然淡出 |

配置:`decay_enabled=1`、`decay_half_life_days=45`、`decay_types="episodic"`(brain.json 可调)。
系数 `0.5^(age/半衰期)` 在 RRF 打分后、排序前乘上;取不到年龄不丢结果。

### 记忆取代(Supersede)

矛盾记忆不再同时注入 —— 高相似度(默认 **0.90**)的一对里,**较新者胜**,旧条打 `superseded_by` 标记,
检索与注入侧自动过滤;`unsupersede <id>` 可无损回滚。

```bash
hermes atbrains-memory supersede-detect                  # 预演(默认,不写库)
hermes atbrains-memory supersede-detect --apply          # 应用
hermes atbrains-memory unsupersede <ID>                  # 回滚
```

### Rerank(默认关闭)

RRF 之后可选加一道 Cross-Encoder 重排(硅基流动 `BAAI/bge-reranker-v2-m3`,实测 216ms)。
实测能把 RRF 分不开的噪声用 **0.92 vs 0.03 的悬崖**切掉。配置 `rerank_enabled=1` 开启,
失败自动回落 RRF 顺序。

### Memory Service(/v1,版本化接口)

把检索逻辑收拢到服务端的契约层。与监控控制台**共用 8787 端口**,令牌隔离(交叉用必 401)。

```
GET  /v1/health
POST /v1/memory/recall     {query, session_id?}
POST /v1/memory/remember   {content, kind?, tags?, importance?, memory_type?}
POST /v1/memory/forget     {id}
POST /v1/memory/recent     {limit?}
POST /v1/memory/turn       {session_id, user, assistant?, platform?, author?, messages?}
GET  /v1/memory/status
```

- 鉴权:`Authorization: Bearer <令牌>`;**服务端用令牌反查 agent 身份,客户端无法伪造**;
  `machine` 记令牌绑定的主机名(/v1)或服务端主机(MCP)。
- 令牌名可写 **`<主机>|<agent>`**(例 `r9000p-Windows|dsh`):`/v1` 左= machine 右= agent,MCP 只取右边当
  agent;老名字不含 `|` 时保持历史口径。
- `/v1/memory/turn` 的 `messages[]` 是「到目前为至的完整会话」,服务端按
  `brain_sessions.archived_messages` 游标只落增量;`assistant` 可为空(只有工具调用的回合)。
  这个端点就是 dsh 原生插件自动归档用的入口(见 `/root/dsh-atbrains`)。
- 令牌:`service-tokens.json`(600);`hermes atbrains token-list|token-add|token-rm`。
- **客户端接入(远程模式)**:brain.json 开 `remote_enabled=1` + `remote_url`;令牌放
  `~/.hermes/.env` 的 `BRAIN_REMOTE_TOKEN`(不落 brain.json)。任何失败**自动回落本地直连**
  (实测死地址 33ms 回落),`remote_timeout_ms=800`。`hermes atbrains-memory remote-check` 自检。

### Redis key 前缀(为摆脱多 DB,默认关闭)

`redis_key_prefix` 为空时行为**逐字节不变**;设成 `brain:server` 即启用前缀隔离。
一次性迁移工具:`python3 /root/agent-db/migrate-redis-prefix.py --prefix brain:server`
(**默认预演**,`--apply` 才改名;目标键已存在会拒绝覆盖并计入冲突)。

---

## 六、日常运维(命令行)

```bash
hermes atbrains-memory status          # 连通性 + 计数总览
hermes atbrains-memory doctor          # 依赖/凭据/连接/表结构自检
hermes atbrains-memory stats           # JSON 格式统计(喂给脚本)
hermes atbrains-memory search <关键词>  # 人工检索
hermes atbrains-memory recent          # Redis 最近对话流
hermes atbrains-memory vector-status   # 向量层:开关/模型/key 来源/已向量化统计
hermes atbrains-memory vector-drain    # 手动增量回填(--force 全量重嵌)
hermes atbrains-memory vector-test     # 对比:关键词 / 向量 / 融合 三种召回
hermes atbrains-memory prune --dry-run # 清理预演(180/365/365 天保留期)
```

**完整性审计**(发现问题时用)
```bash
python3 scripts/audit-turns.py                # 对话归档缺口(只读)
python3 scripts/audit-turns.py --apply        # 回填(只写自己 machine)
python3 scripts/audit-memories.py             # 内置记忆 ↔ brain 镜像对账
```

**自动化(零 systemd)**:向量补漏由**插件内巡检**完成 —— 长命进程(网关)每 60 秒扫一次,短命进程尽力而为后 0.3 秒退出,遗留欠账由下一个长命进程接手(实测 100 秒内补齐)。数据库清理由插件内 `brain-maintenance` 线程负责(每 60 分钟检查,距上次超过 7 天才执行)。手动方式:`hermes atbrains-memory vector-drain` / `hermes atbrains-memory prune --dry-run`。

---

## 七、故障速查

| 症状 | 先查 | 常见原因 |
|---|---|---|
| `brain_*` 工具没了 / 召回为空 | `hermes memory status` | `memory.provider` 被重置,或插件进了 `plugins.disabled` |
| 连接失败 | `hermes atbrains-memory doctor` | 容器挂了:`docker start redis mariadb qdrant` |
| 对话归档缺轮次 | `audit-turns.py` | 配置变更后未重启进程,写入静默失败 |
| 向量不更新 | `hermes atbrains-memory vector-status` + 控制台定时器卡片 | 嵌入服务 key 失效 / 定时器停了 |
| 检索变差 | 控制台「向量覆盖率」 | 有活跃记忆缺向量 → 点「立即回填向量」 |
| 控制台行为与仓库不一致 | `./install.sh check` | 安装副本落后于仓库 → `./install.sh`(然后按提示重启控制台) |

完整清单见 `docs/skill-hermes-brain-memory.md`(含各类已知故障的成因与修复)。

---

## 八、这个仓库的安全约定

- ✅ `docs/scripts/setup` 由 `tools/package.py` 自动**脱敏**生成(密钥 → `***REDACTED***`,生成时复检)
- ✅ 真实密钥只在原位置:见 `setup/SECRETS.md`(只写路径索引)
- ✅ 控制台 token 在**安装副本**的 `console/.token`(权限 600),已被 `.gitignore` 排除——
  **不进 git、不进文档、不进聊天**
- ⚠️ 本目录**可以打包带走**(代码不含密钥);外发前建议再跑一次脱敏复检:
  `python3 tools/package.py` 末尾的复检段,或 `setup/SECRETS.md` 末尾的自检命令
