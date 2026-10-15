# atbrains · 记忆系统资料库

> 本目录汇集了 **brain 双存储记忆系统**(Redis 短期 + MariaDB 长期 + Qdrant 向量)
> 的搭建材料、实现代码、运维脚本,以及一个**可在浏览器里监控和控制它**的控制台。
>
> 整理时间:2026-10-15 · 机器:`server`(腾讯云 Debian 13,2C4G)

---

## 一、系统长什么样

```
                    ┌──────────────────────────────────────────┐
   Hermes 会话 ─────▶│  brain 插件  ~/.hermes/plugins/brain/     │
   (agent)           │  __init__.py  提供者钩子 + 5 个工具        │
                    │  core.py      存储引擎(读写/检索/召回)   │
                    │  cli.py       hermes brain 命令            │
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
              自动化:  brain-vector-drain.timer 每 3 分钟增量回填
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

## 二、目录结构

```
/root/atbrains/
├── README.md                        ← 你正在看的这份
├── docs/                            ← 原始文档与技能手册
│   ├── agent_memory.md              整套记忆系统配置全景(用户维护入口)
│   ├── plugin-README.md             brain 插件自带说明
│   ├── skill-hermes-brain-memory.md 维护手册(故障/修复/整理流程)
│   ├── skill-brain-memory.md        日常使用指南
│   └── brain-overview.txt           库内容快照
├── setup/                           ← 从零重建所需的一切
│   ├── README.md                    重建步骤(按顺序)
│   ├── SECRETS.md                   ★ 机密存放位置(只写路径,不写值)
│   ├── migration-20261012.sql       分库迁移(改名 + 多 agent 分库)
│   ├── init.sql                     空卷初始化
│   ├── rebuild.sh                   容器重建命令
│   ├── verify-accounts.py           账号/权限自检
│   ├── credentials-LEDGER-scrubbed.txt  台账(★已脱敏)
│   └── systemd/                     向量回填的 systemd 单元
├── scripts/                         ← 运维脚本
│   ├── audit-turns.py               对话归档完整性审计/回填
│   ├── audit-memories.py            内置记忆 ↔ brain 镜像对账
│   ├── brain-overview.py            库内容导出
│   ├── prune.sh                     保留期清理
│   └── refresh-hot-machine.py       刷新机器标记
└── plugin → ~/.hermes/plugins/atbrains/   ← ★ 监控控制台(Hermes 插件形态)
    ├── plugin.yaml                  清单(不声明 pip_dependencies)
    ├── __init__.py                  register(ctx):2 个工具 + `hermes atbrains` 命令
    ├── probe.py                     ★ 唯一数据层(CLI/工具/网页共用)
    ├── cli.py / tools.py            CLI 与 agent 工具实现
    ├── console/                     网页控制台(server.py + index.html)
    └── README.md

注:根目录的 `plugin` 是**软链接**(源码必须住在 ~/.hermes/plugins/ 才能被 Hermes 加载);
`web/` 只保留控制台截图与指针说明。
```

---

## 三、监控控制台(Hermes 插件形态)

**形态**:**Hermes 插件** `atbrains`(已 enabled),**不是系统服务** —— 没有 systemd 单元。
控制台由 `hermes atbrains serve` 按需启动、`stop` 停止。

**三个使用面**(共用同一份数据层 `probe.py`,结果不会漂移):

| 面 | 用法 |
|---|---|
| **命令行** | `hermes atbrains status` —— 一屏总览(最常用) |
| **Agent 工具** | `atbrains_status` / `atbrains_control` —— 微信里直接问"记忆系统健康吗" |
| **网页** | `hermes atbrains serve --background` 启动,`open` 打印地址口令,`stop` 停止 |

```bash
hermes atbrains status          # 监控
hermes atbrains serve --background
hermes atbrains open            # 地址 + 口令(SSH 隧道用)
hermes atbrains stop
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

### 能做什么

| 面板 | 内容 |
|---|---|
| **卡片总览** | 三层的计数/状态/容器健康;向量覆盖率(活跃记忆 ↔ 向量点);定时器状态与上次退出码 |
| **记忆** | 全量浏览、按内容过滤、按活跃/停用筛选、标签与权重一览;行内可直接停用(软删除,可恢复) |
| **对话归档** | 按时间倒序查看每轮问答(用户/助手,含来源机与会话) |
| **检索** | **混合检索**:向量(2560 维语义)+ 关键词融合,同时命中记忆与历史对话 |
| **操作** | 立即回填向量 / 系统体检 / 新增记忆 / 归档清理预演 |

自动刷新:总览 10 秒,对话 30 秒(可关)。**只读按钮一律不写库**;清理只做预演。

### 后端接口(想自己接脚本时用)

```
GET  /api/overview                 三层总览(容器/计数/向量/定时器)
GET  /api/memories?limit=&offset=&active=1|0|all&q=
GET  /api/turns?limit=
GET  /api/search?q=&limit=         混合检索
GET  /api/recent?limit=            Redis 热点 + 最近对话
POST /api/remember   {content,kind,importance,tags}
POST /api/forget     {id}
POST /api/drain                    触发向量回填
POST /api/doctor                   系统体检
POST /api/prune      {dry_run:true}
```

> 设计原则:**凭据只在服务端解析,绝不下发浏览器**;所有 `/api/*` 都要 token。

---

## 四、日常运维(命令行)

```bash
hermes brain status          # 连通性 + 计数总览
hermes brain doctor          # 依赖/凭据/连接/表结构自检
hermes brain stats           # JSON 格式统计(喂给脚本)
hermes brain search <关键词>  # 人工检索
hermes brain recent          # Redis 最近对话流
hermes brain vector-status   # 向量层:开关/模型/key 来源/已向量化统计
hermes brain vector-drain    # 手动增量回填(--force 全量重嵌)
hermes brain vector-test     # 对比:关键词 / 向量 / 融合 三种召回
hermes brain prune --dry-run # 清理预演(180/365/365 天保留期)
```

**完整性审计**(发现问题时用)
```bash
python3 scripts/audit-turns.py                # 对话归档缺口(只读)
python3 scripts/audit-turns.py --apply        # 回填(只写自己 machine)
python3 scripts/audit-memories.py             # 内置记忆 ↔ brain 镜像对账
```

**自动化**:`brain-vector-drain.timer` 每 3 分钟跑一次增量回填,新记忆自动向量化。

---

## 五、故障速查

| 症状 | 先查 | 常见原因 |
|---|---|---|
| `brain_*` 工具没了 / 召回为空 | `hermes memory status` | `memory.provider` 被重置,或插件进了 `plugins.disabled` |
| 连接失败 | `hermes brain doctor` | 容器挂了:`docker start redis mariadb qdrant` |
| 对话归档缺轮次 | `audit-turns.py` | 配置变更后未重启进程,写入静默失败 |
| 向量不更新 | `hermes brain vector-status` + 控制台定时器卡片 | 嵌入服务 key 失效 / 定时器停了 |
| 检索变差 | 控制台「向量覆盖率」 | 有活跃记忆缺向量 → 点「立即回填向量」 |

完整清单见 `docs/skill-hermes-brain-memory.md`(含 6 类已知故障的成因与修复)。

---

## 六、这个目录的安全约定

- ✅ 所有文件已**自动脱敏**(23 处密钥替换为 `***REDACTED***`)
- ✅ 真实密钥仍只在原位置:见 `setup/SECRETS.md`
- ✅ 控制台 token 单独存放(`web/.token`,权限 600),不进代码、不进文档
- ⚠️ 本目录**可以打包带走**,但拿走前建议再跑一次脱敏复检:
  `python3 -c "..."`(见 setup/SECRETS.md 末尾的自检命令)
