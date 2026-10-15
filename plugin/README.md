# atbrains —— Hermes 插件

brain 记忆系统(Redis 短期 + MariaDB 长期 + Qdrant 向量)的**监控与控制台**。
以 **Hermes 插件**形态交付:**不注册任何 systemd 服务**,由 Hermes 加载、按需启停。

> **代码的家是仓库** `/root/atbrains/plugin/`(git 管理);
> `~/.hermes/plugins/atbrains` 是**安装副本**(Hermes 实际加载的位置)。
> 改动流程:改仓库 → `./install.sh` → 需要时 `hermes atbrains stop && serve --background` 重启控制台。
> 直接改安装副本会在下次 `install.sh` 时被覆盖。

```
hermes plugins enable atbrains     # 启用(已启用)
hermes plugins info atbrains       # 查看状态
```

## 三个使用面(共用同一份数据层 `probe.py`,结果不会漂移)

### 1. 命令行

```bash
hermes atbrains status                          # 一屏总览(最常用)
hermes atbrains serve [--port N]                # 前台启动网页控制台
hermes atbrains serve --background              # 后台启动(写 pidfile)
hermes atbrains stop                            # 停掉后台控制台
hermes atbrains open                            # 打印地址与口令(配 SSH 隧道)
hermes atbrains drain [--force]                 # 手动向量回填(增量/全量)
hermes atbrains doctor                          # 系统体检
hermes atbrains memories [--all] [--q 词] [--limit N]
hermes atbrains turns [--limit N] [--machine M] [--q 词]   # 最近对话归档
hermes atbrains sessions [--limit N] [--machine M]         # 会话索引
hermes atbrains search <关键词> [--limit N]      # 混合检索(向量+关键词)
hermes atbrains remember <内容> [--kind fact] [--tags a,b] [--importance 8]
hermes atbrains forget <ID>                     # 停用一条记忆(软删除)
hermes atbrains restore <ID>                    # 恢复一条被停用的记忆
```

### 2. Agent 工具(微信/CLI 里直接问就行)

| 工具 | 作用 |
|---|---|
| `atbrains_status` | 总览:三层计数、容器健康、向量覆盖、定时器状态 |
| `atbrains_control` | 动作:`drain` / `doctor` / `remember` / `forget` / `restore` / `prune` / `recent` |

例:"记忆系统健康吗" → `atbrains_status`;"把这条记下来" → `atbrains_control(action=remember)`;
"停用/恢复 #42 这条记忆" → `atbrains_control(action=forget|restore, id=42)`。

### 3. 网页控制台(按需启动)

```bash
hermes atbrains serve --background
# SSH 隧道(推荐,不用动安全组):
ssh -p 7622 -L 8787:127.0.0.1:8787 root@<服务器>
# 浏览器 → http://127.0.0.1:8787/
#   口令: cat ~/.hermes/plugins/atbrains/console/.token(粘入右上角,回车即保存)
#   免输入直连: http://127.0.0.1:8787/?token=<口令>   ← 打开即登录(参数自动抹掉)
hermes atbrains stop
```

面板(v1.1):

| 面板 | 内容 |
|---|---|
| **卡片总览** | 三层计数/状态/容器健康;向量覆盖率(含进度条与待回填数);定时器上次/下次运行与退出码 |
| **记忆** | 内容/类型/状态过滤 + 分页;点击行看全文;行内停用(软删除)与**恢复**;详情弹窗含时间戳与复制 |
| **对话归档** | 来源机/关键词过滤 + 分页 + 按会话精查;详情弹窗看完整问答(含截断标记) |
| **会话** | 会话索引(轮次/开始/最近活动),一键跳到该会话的对话轮次 |
| **动态** | Redis 短期层实时视图:热点记忆 + 最近对话流 |
| **检索** | 混合检索:向量(2560 维语义)+ 关键词融合,同时命中记忆与历史对话 |
| **操作** | 回填向量 / 体检 / 新增记忆 / 按 ID 查看·停用·恢复 / 归档清理预演(不写库) |

自动刷新:跟随右上角设置(5s/10s/30s/关),页面不可见时暂停;对话页可见时每 30 秒自动补刷。

### 后端接口(想自己接脚本时用)

```
GET  /api/overview                 三层总览(容器/计数/向量/定时器/版本)
GET  /api/memories?limit=&offset=&active=1|0|all&q=&kind=&machine=
GET  /api/memory?id=               单条记忆全量(详情弹窗用)
GET  /api/turns?limit=&offset=&machine=&session_id=&q=
GET  /api/turn?id=                 单轮对话全量(详情弹窗用)
GET  /api/sessions?limit=&offset=&machine=&q=
GET  /api/search?q=&limit=         混合检索
GET  /api/recent?limit=            Redis 热点 + 最近对话
POST /api/remember   {content,kind,importance,tags}
POST /api/forget     {id}          软删除
POST /api/restore    {id}          恢复(失败时返回 {ok:false,error})
POST /api/drain                    触发向量回填
POST /api/doctor                   系统体检
POST /api/prune      {dry_run:true}
```

> 设计原则:**凭据只在服务端解析,绝不下发浏览器**;所有 `/api/*` 都要 token。

## 结构

```
atbrains/plugin/               ← 仓库中的真实代码(git 管理)
├── plugin.yaml        清单(name/version/provides_tools;刻意不声明 pip_dependencies)
├── __init__.py        register(ctx):注册 2 个工具 + `hermes atbrains` 命令
├── probe.py           ★ 唯一数据层(CLI/工具/网页共用);brain 插件路径自动定位
├── cli.py             `hermes atbrains` 实现
├── tools.py           agent 工具实现
├── console/           网页控制台(server.py + index.html + config.json;.token 运行时生成)
└── README.md
```

## 设计决策(为什么这么写)

- **不声明 `pip_dependencies`** —— 只用 stdlib + 兄弟插件 `brain` 的 vendor 库。
  一旦声明依赖,PM 会把插件并入 uv workspace 重解析,而国内镜像缺 PEP 700 元数据
  会让解析失败、插件被自动禁用(本机踩过)。
- **不注册 systemd** —— 控制台用 `serve` 按需起、`stop` 停;进程生命周期跟着用户的显式命令走。
- **单一数据层** —— 三个使用面都调 `probe.py`,避免"网页显示 65 条、CLI 显示 64 条"这类漂移。
- **凭据不出服务端** —— 网页只拿到结果数据,DB/Qdrant 口令在服务端解析。
- **依赖兄弟插件** —— 通过 brain 的 `core.BrainStore` 读写,复用其凭据解析与重试逻辑;
  brain 插件缺失时报错清晰(`ProbeError`)而不是静默失败。brain 位置按
  `ATBRAINS_BRAIN_PLUGIN` 环境变量 → 与插件并排目录 → `HERMES_HOME/plugins/brain` 依次探测。
- **恢复(restore)与停用对偶** —— 都是「改 MariaDB active + 维护 Redis 热点镜像」;
  恢复会拒绝"同内容已有活跃条目"(库有唯一键防重),向量由定时器 ≤3 分钟补回。

## 依赖

- 必须启用 `brain` 插件(数据源),且三个容器在跑:`redis` / `mariadb` / `qdrant`
- 网页控制台需要 Python 3(与 Hermes 同解释器)
