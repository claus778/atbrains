# atbrains —— Hermes 插件

brain 记忆系统(Redis 短期 + MariaDB 长期 + Qdrant 向量)的**监控与控制台**。
以 **Hermes 插件**形态交付:**不注册任何 systemd 服务**,由 Hermes 加载、按需启停。

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
hermes atbrains turns [--limit N]               # 最近对话归档
hermes atbrains search <关键词> [--limit N]      # 混合检索(向量+关键词)
hermes atbrains remember <内容> [--kind fact] [--tags a,b] [--importance 8]
hermes atbrains forget <ID>                     # 停用一条记忆(软删除)
```

### 2. Agent 工具(微信/CLI 里直接问就行)

| 工具 | 作用 |
|---|---|
| `atbrains_status` | 总览:三层计数、容器健康、向量覆盖、定时器状态 |
| `atbrains_control` | 动作:`drain` / `doctor` / `remember` / `forget` / `prune` / `recent` |

例:"记忆系统健康吗" → `atbrains_status`;"把这条记下来" → `atbrains_control(action=remember)`。

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

面板:卡片总览(三层+覆盖率+定时器) · 记忆浏览(行内可直接停用,软删除) · 对话归档 · 混合检索 · 操作(回填/体检/新增/清理预演)。

## 结构

```
~/.hermes/plugins/atbrains/
├── plugin.yaml        清单(name/version/provides_tools;刻意不声明 pip_dependencies)
├── __init__.py        register(ctx):注册 2 个工具 + `hermes atbrains` 命令
├── probe.py           ★ 唯一数据层(CLI/工具/网页共用)
├── cli.py             `hermes atbrains` 实现
├── tools.py           agent 工具实现
├── console/           网页控制台(server.py + index.html + config.json + .token)
└── README.md
```

## 设计决策(为什么这么写)

- **不声明 `pip_dependencies`** —— 只用 stdlib + 兄弟插件 `brain` 的 vendor 库。
  一旦声明依赖,PM 会把插件并入 uv workspace 重解析,而国内镜像缺 PEP 700 元数据
  会让解析失败、插件被自动禁用(本机踩过)。
- **不注册 systemd** —— 控制台用 `serve` 按需起、`stop` 停;进程生命周期跟着用户的显式命令走。
- **单一数据层** —— 三个使用面都调 `probe.py`,避免"网页显示 65 条、CLI 显示 64 条"这类漂移。
- **凭据不出服务端** —— 网页只拿到结果数据,DB/Qdrant 口令在服务端解析。
- **依赖兄弟插件** —— 通过 `../brain` 的 `core.BrainStore` 读写,复用其凭据解析与重试逻辑;
  brain 插件缺失时报错清晰(`ProbeError`)而不是静默失败。

## 依赖

- 必须启用 `brain` 插件(数据源),且三个容器在跑:`redis` / `mariadb` / `qdrant`
- 网页控制台需要 Python 3(与 Hermes 同解释器)
