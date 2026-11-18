# 从零搭建 atbrains（完整步骤）

> 目标机器：Debian 13 / Ubuntu 22.04+，2C4G 起。以下命令都在 `/root` 下执行。
> 全部口令用 `<...>` 占位；真实值只放 `~/.hermes/.env` 与 qdrant 配置文件，**不进 git、不进聊天**。
> 表/列含义见 [`../docs/database-schema.md`](../docs/database-schema.md)；`/v1` 接口见 [`../docs/protocol-v1.md`](../docs/protocol-v1.md)。

---

## 0. 这套系统由什么组成

```
Hermes 会话 ──┐                        ┌─ MariaDB  :57306  agent_memory（14 表，全局权威）
dsh 会话 ─────┼── /v1 或 /mcp ──▶ 8787 ─┼─ Redis    :9079   db0（短期：流/热点/缓存）
其它 agent ───┘   （Bearer 令牌）      └─ Qdrant   :6333   brain_memories（向量，可重建）
                       │
                       └─ atbrains-memory（Python 引擎，Hermes 插件形态）——
                          召回 / 记忆 / 归档 / 知识层 / 向量回填 / 清理，全部在它里面
```

| 组件 | 形态 | 端口 | 作用 |
|---|---|---|---|
| Redis | Docker | 9079 | 短期层：最近对话流、会话状态、热点镜像、检索缓存 |
| MariaDB | Docker | 57306 | 长期层：记忆 / 对话 / 会话 / 存档 / 知识层 / 思考与工具明细 |
| Qdrant | Docker | 6333/6334 | 向量层：记忆正文的 2560 维向量 |
| atbrains 控制台 | systemd | 8787 | 网页面板 + `/v1`（HTTP 契约）+ `/mcp`（MCP 端点） |
| atbrains-memory | Hermes 插件 | — | 记忆引擎本体；控制台也 import 它 |

---

## 1. 前置

```bash
docker --version && systemctl is-active docker
python3 -V                      # 3.11+ 即可；Hermes 自带解释器也行
ss -lntp | grep -E ':(9079|57306|6333|8787)\b' || echo "端口空闲"
```

目录约定（数据必须落在宿主机目录，容器重建不丢）：

```bash
mkdir -p /root/redis /root/mariadb /root/qdrant/qdrant-config /root/agent-db /root/atbrains
```

---

## 2. 起三个容器

**Redis（短期层）**

```bash
docker run -d --name redis --restart unless-stopped \
  -p 9079:6379 \
  -v /root/redis:/data \
  redis:latest \
  redis-server --appendonly yes --requirepass '<REDIS_PASSWORD>'
```

**MariaDB（长期层）**

```bash
# 时区口径：容器内固定 UTC（全库时间都按 UTC 存）
mkdir -p /root/agent-db/mysql-conf.d
cat > /root/agent-db/mysql-conf.d/99-timezone.cnf <<'EOF'
[mysqld]
default-time-zone = '+00:00'
EOF

docker run -d --name mariadb --restart unless-stopped \
  -p 57306:3306 \
  -v /root/mariadb:/var/lib/mysql \
  -v /root/agent-db/mysql-conf.d/99-timezone.cnf:/etc/mysql/conf.d/99-timezone.cnf:ro \
  -v /root/agent-db/init.sql:/docker-entrypoint-initdb.d/10-agents.sql:ro \
  -e MARIADB_ROOT_PASSWORD='<ROOT_PASSWORD>' \
  -e MARIADB_DATABASE=agent_memory \
  -e MARIADB_USER=agent \
  -e MARIADB_PASSWORD='<MARIADB_PASSWORD>' \
  mariadb:latest \
  --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci
```

> `10-agents.sql` 只在**空数据目录首次启动**时执行；已有 `/root/mariadb` 时不会跑，属正常。

**Qdrant（向量层）**

```bash
# 先把 key 写进配置文件（容器 env 里不放 key）
cat > /root/qdrant/qdrant-config/production.yaml <<'EOF'
service:
  api_key: "<QDRANT_AND_EMBED_KEY>"
EOF

docker run -d --name qdrant --restart unless-stopped \
  -p 6333:6333 -p 6334:6334 \
  -e TZ=Etc/UTC \
  -v /root/qdrant:/qdrant/storage \
  -v /root/qdrant/qdrant-config/production.yaml:/qdrant/config/production.yaml:ro \
  qdrant/qdrant
```

> ⚠️ 这把 `api_key` **当前实现里同时用作两处**：qdrant 鉴权 + 硅基流动 embed key（用户指定同串）。
> 轮换 = 改这个文件 + `docker restart qdrant`；插件有 5 分钟缓存，401 会自动失效重读。

---

## 3. 建库 + 表结构

两种方式，二选一：

**A. 让插件自动建（推荐）** —— 装好插件后第一次 `doctor` / `status` 就会幂等建出 14 表 / 10 视图 / 10 触发器。

**B. 手工导入（审计 / 离线重建用）**

```bash
docker exec -i mariadb sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -e "
  CREATE DATABASE IF NOT EXISTS agent_memory CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"'

docker exec -i mariadb sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" agent_memory' \
  < setup/schema-v9.sql

# 核对：14 表 / 10 视图 / 10 触发器
docker exec -i mariadb sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -N -B -e "
  SELECT TABLE_TYPE, COUNT(*) FROM information_schema.tables WHERE TABLE_SCHEMA=\"agent_memory\" GROUP BY 1;
  SELECT COUNT(*) FROM information_schema.triggers WHERE TRIGGER_SCHEMA=\"agent_memory\";"'
```

`setup/schema-v9.sql` 由 `tools/export-schema.py` 从活库生成（已剥离 DEFINER、不含 AUTO_INCREMENT 计数），
改完结构重新生成即可。旧文件 `init.sql.old-20261025` 是 v8 之前的分库方案，仅作考古。

---

## 4. 装 Hermes 记忆插件

```bash
cd /root/atbrains
./install.sh            # rsync: atbrains-memory/ → ~/.hermes/plugins/atbrains-memory
                        #        plugin/         → ~/.hermes/plugins/atbrains
                        # 保留运行时文件：service-tokens.json / console/.token

# 备选：install.sh 想顺手重启网关被 lifecycle guard 拒绝时，手动两条
rsync -a --delete --exclude '__pycache__' atbrains-memory/ ~/.hermes/plugins/atbrains-memory/
rsync -a --delete --exclude '__pycache__' --exclude 'service-tokens.json' --exclude '.token' \
      plugin/ ~/.hermes/plugins/atbrains/
```

**两条硬约束（踩过的坑）**

1. `plugin.yaml` **绝不要声明 `pip_dependencies`** —— 会被并进 uv workspace 重解析，
   国内镜像缺 PEP 700 元数据 → 解析失败 → 插件被自动禁用。依赖用 `vendor/` 内置的 redis + pymysql。
2. 别改安装副本再指望生效：改 `/root/atbrains/` 里的源码，再 rsync 过去。

---

## 5. 配置 + 激活

**非密配置** `~/.hermes/atbrains.json`（插件与 `hermes atbrains-memory` 共用）：

```json
{
  "machine": "server",
  "redis_host": "127.0.0.1", "redis_port": 9079, "redis_db": 0,
  "mariadb_host": "127.0.0.1", "mariadb_port": 57306,
  "mariadb_user": "agent", "mariadb_db": "agent_memory",
  "vector_enabled": 1,
  "embed_model": "Qwen/Qwen3-Embedding-4B", "embed_dim": 2560,
  "qdrant_url": "http://127.0.0.1:6333", "qdrant_collection": "brain_memories",
  "qdrant_config_file": "/root/qdrant/qdrant-config/production.yaml",
  "vector_topk": 12, "vector_min_score": 0.25,
  "rerank_enabled": 0, "distill_enabled": 0
}
```

**口令只进** `~/.hermes/.env`（用 Hermes 的 `save_env_value` 写，别手写引号）：

```
BRAIN_REDIS_PASSWORD=<REDIS_PASSWORD>
BRAIN_MARIADB_PASSWORD=<MARIADB_PASSWORD>
```
（`ATBRAINS_REDIS_PASSWORD` / `ATBRAINS_MARIADB_PASSWORD` 是等价别名，二选一。）

**激活 provider**

```bash
hermes config set memory.provider atbrains-memory
hermes memory status                 # 应显示 atbrains-memory, available ✓
hermes atbrains-memory doctor        # 依赖 ✓ 凭据 ✓ 连接 ✓ 表结构 v9
```

---

## 6. 控制台 / `/v1` / `/mcp`（8787）

```bash
# 1) 令牌表（每个客户端一条；名字格式 <主机>|<agent>，左=machine 右=agent）
hermes atbrains token-add server
hermes atbrains token-add 'r9000p-Windows|dsh'
hermes atbrains token-list

# 2) 常驻（必须 systemd：插件自带 serve --background 是网关子进程，回合结束就被清掉）
cp setup/systemd/atbrains-console.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now atbrains-console
systemctl is-active atbrains-console
tail -5 /var/log/atbrains-console.log

# 3) 自检
curl -s http://127.0.0.1:8787/v1/health                  # {"ok":true,"version":"2.0.0",...}
python3 scripts/contract-v1.py                            # 21 项契约测试（含写库 + 游标幂等）
python3 /root/agent-db/mcp_client_test.py --url http://127.0.0.1:8787/mcp --agent omp   # tools=10
```

> `atbrains-console.service` 的 `ExecStart` 指向 `console/run.sh`，里面用 glob 取最新的
> `/root/.hermes/tools/python-3.*/bin/python3` —— **别把版本号写死**，Hermes 一升级路径就变
> （症状：`status=203/EXEC`、端口静默不监听）。

---

## 7. dsh 客户端（可选）

```bash
# 包在 dist/，源码在 clients/dsh-atbrains/
# 目标机（Windows）：解包到 %USERPROFILE%\.dsh\plugins\dsh-atbrains
#   setx ATBRAINS_MCP_TOKEN "<该机令牌>"
#   cordis.patch.yml 里 baseUrl 指向 http://<服务器>:8787
# 重启 dsh → 日志出现 [atbrains] 已启用 → …（召回 + 自动归档）
```

细节见 `dist/dsh-onboarding.md`。它每轮自动召回，并把完整消息链（含工具调用/推理）归档进
`brain_turns` / `brain_turn_messages`，不经过 Python。

---

## 8. astrbot 客户端（可选）

AstrBot（QQ 机器人框架，Python，`astrbot_version >= 4.0.0`）装的是**原生插件**：
`clients/astrbot-atbrains/`，自带 `vendor/` 的 pymysql + redis，**直连** MariaDB:57306
与 Redis:9079 —— **不走 `/v1`、不走 `/mcp`**。所以跑 AstrBot 的那台机器要能网络可达
这两个端口（跨公网时把配置指到公网地址，安全组/家宽都要放行）。

```bash
# 方式 A：打包分发（推荐，产物在 dist/）
python3 tools/package-astrbot-client.py          # → dist/astrbot-atbrains-1.0.0.zip
unzip -o dist/astrbot-atbrains-1.0.0.zip -d /opt/astrbot/data/plugins/
# 解包出来的目录名就是插件 id（atbrains-memory），无需再改名

# 方式 B：直接从仓库拷贝源码（服务器或 rsync 到目标机）
cp -r clients/astrbot-atbrains /opt/astrbot/data/plugins/atbrains-memory
chown -R root:root /opt/astrbot/data/plugins/atbrains-memory
systemctl restart astrbot                        # 或用 WebUI 的插件热重载
```

配置复用 Hermes 侧那一份（插件自己不另存密钥）：

- `~/.hermes/atbrains.json` —— host/port/db/machine；**AstrBot 不在服务器上时，
  把 `mariadb_host` / `redis_host` 改成服务器的公网地址**（端口仍是 57306 / 9079）
- `~/.hermes/.env` —— `ATBRAINS_MARIADB_PASSWORD` / `ATBRAINS_REDIS_PASSWORD`
  （兼容旧名 `BRAIN_MARIADB_PASSWORD` / `BRAIN_REDIS_PASSWORD`）；
  `ATBRAINS_HERMES_HOME` 可覆盖配置目录（默认 `~/.hermes`）

自检（只读，不改数据）。分两种情况：

```bash
# AstrBot 机器上（用 AstrBot 自带 venv 的解释器；路径按该机实际改）
cd /opt/astrbot/data/plugins/atbrains-memory
/opt/astrbot/venv/bin/python main.py --selftest     # deps / 连接 / 开关 / search / prefetch
journalctl -u astrbot -f | grep atbrains
#   [atbrains] 已就绪：machine=… mariadb=…:57306 redis=…:9079/db0 agent=astrbot   ← 启动
#   [atbrains] 已注入召回 N 条（…）                                                ← 每轮
#   [atbrains] 已归档回合 #id（qq · …）                                            ← 每轮

# 没有 AstrBot 的机器（装前预检 / 排障）—— 系统 python3 也能跑：
python3 main.py --selftest
#   [atbrains] 未检测到 AstrBot 运行环境，使用占位实现（仅 --selftest 可用）
#   ↑ 软导入兜底：astrbot 包缺失时退化为占位实现，自检照跑（只读）。
#     在 AstrBot 进程内不会出现这行，钩子注册仍走真 filter/Star。
```

> 注意：**别用系统 `python3` 跑 AstrBot 机器上的插件**（除非该机系统 python 恰好装了 astrbot）——
> astrbot 包只在 AstrBot 自带 venv 里，用错解释器会走到占位实现、钩子不会注册。
> 反过来，没有 AstrBot 的机器上现在两条都能跑：venv 那条报错就用 `python3`。

跨公网时每轮成本 ≈ 往返次数 × RTT（服务器↔国内家宽 40~65 ms 量级）：插件默认把轮首召回
合到 **1 次**往返、轮尾归档合到 **2 次**（MariaDB 1 + Redis 1，且在后台跑不挡收尾），
轮首召回有 **900 ms 硬预算**（超时跳过注入并记 warn）。开关与默认值见
`clients/astrbot-atbrains/README.md` 的配置表。

---

## 9. 验收清单（逐层，全过才算搭好）

```bash
hermes atbrains-memory doctor          # 依赖 / 凭据 / Redis / MariaDB / 表结构 v9
hermes atbrains-memory status          # 计数总览（记忆 / 对话 / 会话 / 转录 / 占用）
hermes atbrains-memory vector-status   # 开关 / 模型 / key 来源 / 缺失·失败·过期应全 0
python3 scripts/contract-v1.py         # /v1 契约（HTTP + 库断言 + 自动清理）
curl -s http://127.0.0.1:8787/v1/health

# 端到端：新会话只凭记忆回答一个只有库里知道的事实，应 0 次工具调用就答对
hermes chat -q "只凭记忆回答：<只存在于库里的那个事实>"
```

浏览器验收（可选）：`ssh -p 7622 -L 8787:127.0.0.1:8787 root@<服务器>`，
再开 `http://127.0.0.1:8787/?token=<console/.token 里的值>`。

---

## 10. 日常维护

| 事 | 命令 |
|---|---|
| 备份 | `mariadb-dump ... agent_memory \| gzip > backup-$(date +%F).sql.gz`，再 `gzip -t` 验一遍 |
| 恢复 | `zcat backup.sql.gz \| mariadb ... agent_memory` |
| 清理 | `python3 /root/agent-db/prune.sh`（插件内也有维护线程，每 7 天一次） |
| 向量补漏 | 长命进程每 60 秒自动扫；手动 `hermes atbrains-memory vector-drain` |
| 升级插件 | 仓库 `git pull` → `./install.sh` → `systemctl restart atbrains-console` → 重开 Hermes 会话 |
| 重建触发器 | `python3 /root/agent-db/apply-platform-triggers.py && python3 /root/agent-db/apply-author-rules.py` |
| 结构导出 | `python3 tools/export-schema.py > setup/schema-v9.sql` |

> **不要**给向量/清理再加 systemd 定时器：它们都在插件进程内（用户明确要求不依赖系统服务）。
> 唯一的 systemd 单元是控制台（8787），因为它是长命进程。

---

## 11. 故障速查

| 现象 | 先查 |
|---|---|
| `memory.provider` 变空 / 插件被 disabled | `plugin.yaml` 是否混进了 `pip_dependencies`；`hermes config set memory.provider atbrains-memory` |
| `doctor` 报连接失败 | 容器是否在跑；`.env` 口令是否被引号污染（用 Python 解析，别 `source .env`） |
| 召回为空、但工具在 | provider 是否 active；`brain_memories.active` 是否有行；向量层是否回落关键词 |
| `brain_turns` 没有新行 | 配置变更后是否重启过进程；`grep "sync_turn 写入失败" ~/.hermes/logs/agent.log` |
| 8787 不监听 | `systemctl status atbrains-console`；`run.sh` 里 python 路径是否失效（203/EXEC） |
| `/v1` 401 | 令牌是否在 `service-tokens.json` 里；`hermes atbrains token-list` 对照 |
| dsh 有工具但从不调用 | MCP 只提供工具；自动召回要装 `clients/dsh-atbrains` 原生插件 |
| 表结构版本不对 | `SELECT meta_value FROM brain_meta WHERE meta_key='schema_version';`（不是列名） |

---

## 附：目录对照

```
atbrains-memory/        Hermes 记忆插件（引擎本体：core.py / cli.py / vendor / 自带 skill / CONVENTIONS）
plugin/                 控制台 + /v1 + /mcp（server.py、service.py、atbrains_mcp.py、probe.py、console/）
clients/dsh-atbrains/   dsh 原生插件（纯 Node，走 /v1 + /mcp）
clients/astrbot-atbrains/  AstrBot 原生插件（Python，自带 pymysql/redis 直连 MariaDB:57306 + Redis:9079）
docs/                   database-schema.md（表/列字典）、protocol-v1.md（契约）、architecture*、skill 副本
scripts/                contract-v1.py（契约测试）等
setup/                  本文件、schema-v9.sql、init.sql、SECRETS.md、systemd/、verify-accounts.py
tools/                  export-schema.py、package.py（打包/脱敏）、package-astrbot-client.py（出 astrbot 客户端 zip）
web/                    前端静态资源与控制台截图
```
