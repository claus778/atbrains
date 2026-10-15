# 从零重建 brain 记忆系统

> 按顺序执行。**所有口令用占位符表示,真实值见 `SECRETS.md` 指出的位置。**
> 重建前先记下两边的计数(记忆/轮次/会话、Redis DBSIZE),便于重建后核对。

---

## 0. 前置

```bash
# Docker 已装;本机为腾讯云 Debian 13
docker --version && systemctl is-active docker
```

## 1. 起三个容器

**Redis(短期层)** —— `:9079` → 容器内 6379,需 `--requirepass`
```bash
docker run -d --name redis --restart unless-stopped \
  -p 9079:6379 \
  -v redis_data:/data \
  redis:latest \
  redis-server --appendonly yes --requirepass '<REDIS_PASSWORD>'
```

**MariaDB(长期层)** —— `:57306` → 容器内 3306
```bash
docker run -d --name mariadb --restart unless-stopped \
  -p 57306:3306 \
  -v mariadb_data:/var/lib/mysql \
  -e MARIADB_ROOT_PASSWORD='<MARIADB_ROOT_PASSWORD>' \
  -e MARIADB_DATABASE=hermes_agent_memory \
  -e MARIADB_USER=agent \
  -e MARIADB_PASSWORD='<MARIADB_PASSWORD>' \
  mariadb:latest \
  --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci
```

**Qdrant(向量层)** —— `:6333`(HTTP)/ `:6334`(gRPC)
```bash
mkdir -p /root/qdrant/qdrant-config
# 先写好 /root/qdrant/qdrant-config/production.yaml(见第 5 步,内含 service.api_key)

docker run -d --name qdrant --restart unless-stopped \
  -p 6333:6333 -p 6334:6334 \
  -e TZ=Etc/UTC \
  -v /root/qdrant:/qdrant/storage \
  -v /root/qdrant/qdrant-config/production.yaml:/qdrant/config/production.yaml:ro \
  qdrant/qdrant
```

> ⚠️ 数据都在 **named volume**(`redis_data` / `mariadb_data`)和宿主机目录 `/root/qdrant` 里。
> 重建容器**必须挂回同名卷**,否则数据全丢。

## 2. 初始化数据库

```bash
# 空卷初始化(全新部署)
docker exec -i mariadb sh -c 'exec mariadb -u root -p"$MARIADB_ROOT_PASSWORD"' < init.sql

# 或:从旧库迁移 + 拆出多 agent 独立库
docker exec -i mariadb sh -c 'exec mariadb -u root -p"$MARIADB_ROOT_PASSWORD"' < migration-20261012.sql
```

`init.sql` 建表:`brain_memories` / `brain_turns` / `brain_sessions` / `brain_archive`。

**多 agent 分库**(omp / dsh / artex 各自独立库 + 用户 + Redis db5-7):
见 `migration-20261012.sql` 后半段;账号自检用 `verify-accounts.py`。
Redis 库号规划:**db0-4 留给 hermes 系**(server=db0、x260=db1…),非 hermes 从 db5 起。

## 3. 部署 brain 插件

```bash
# 插件目录(Windows 是 %USERPROFILE%\.hermes\plugins\brain)
ls ~/.hermes/plugins/brain/{__init__.py,core.py,cli.py,plugin.yaml,vendor,skills}
```

**关键约束(踩过的坑)**

- `plugin.yaml` **绝不能**声明 `pip_dependencies` —— 一旦声明,PM 会把插件并入 uv
  workspace 重解析,而腾讯 pip 镜像缺 PEP 700 元数据(uv 的 `exclude-newer` 需要),
  解析必失败 → 插件被自动驱逐。
- 依赖必须保持 **vendor 方式**:`vendor/` 里内置纯 Python 的 `redis` + `pymysql`。
- 插件目录损坏时,可用分发包 `dist/brain-plugin-<日期>.zip` 恢复。

## 4. 配置

```bash
# 非密配置
cat ~/.hermes/brain.json
```

```json
{
  "redis_host": "127.0.0.1", "redis_port": 9079, "redis_db": 0,
  "mariadb_host": "127.0.0.1", "mariadb_port": 57306,
  "mariadb_user": "agent", "mariadb_db": "hermes_agent_memory",
  "machine": "server",
  "vector_enabled": 1,
  "embed_model": "Qwen/Qwen3-Embedding-4B",
  "embed_dim": 2560,
  "qdrant_url": "http://127.0.0.1:6333",
  "qdrant_collection": "brain_memories",
  "vector_topk": 12,
  "vector_min_score": 0.25
}
```

```bash
# 密钥只进 .env(用 save_env_value 写,别手写引号 —— 引号会让某些读取方式变形)
#   BRAIN_REDIS_PASSWORD / BRAIN_MARIADB_PASSWORD / (嵌入服务的 KEY)
```

**激活 provider**
```bash
hermes config set memory.provider brain
hermes memory status      # 应显示 brain available
```

## 5. 向量层

**Qdrant 访问口令**:写在 `/root/qdrant/qdrant-config/production.yaml`(只读挂进容器)
```yaml
service:
  api_key: "<QDRANT_API_KEY>"
```
> 容器 env 里**不放**任何 key;仅通过该配置文件。验证:无 key → 401,带 key → 200。

**嵌入模型**:硅基流动 `Qwen/Qwen3-Embedding-4B`(2560 维,支持降维 64~2560)。
key 位置见 `SECRETS.md`。

**建集合**(插件会在首次回填时自动建,也可手动):
```bash
KEY=$(grep -oP '(?<=api_key: ).*' /root/qdrant/qdrant-config/production.yaml)
curl -H "api-key: $KEY" http://127.0.0.1:6333/collections/brain_memories
```

## 6. 自动化(向量增量回填)

```bash
cp systemd/brain-vector-drain.service systemd/brain-vector-drain.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now brain-vector-drain.timer
systemctl list-timers brain-vector-drain.timer
```

每 3 分钟跑一次 `hermes brain vector-drain`(增量,只补缺)。

## 7. 验证(逐层)

```bash
hermes brain doctor                       # 依赖/凭据/连接/表结构
hermes brain status                       # 计数总览
hermes brain vector-status                # 向量层开关/模型/key 来源
hermes brain vector-test                  # 关键词 vs 向量 vs 融合
# 端到端:新会话里"只凭记忆回答 <只有库里知道的事实>",应在 0 工具调用下答对

# 完整性(可选,发现缺口时)
python3 audit-turns.py                    # 对话归档对账
python3 audit-memories.py                 # 内置记忆 ↔ brain 镜像对账
```

## 8. 监控控制台(Hermes 插件)

```bash
# 插件必须装在 Hermes 的插件目录(只有那里会被加载)
cp -r atbrains-plugin ~/.hermes/plugins/atbrains
hermes plugins enable atbrains
hermes plugins info atbrains        # Status: enabled

# 三个使用面(共用 probe.py 数据层)
hermes atbrains status              # 命令行总览
hermes atbrains serve --background  # 按需起网页控制台(不是系统服务)
hermes atbrains open                # 地址 + 口令
hermes atbrains stop                # 停
```

Agent 工具 `atbrains_status` / `atbrains_control` 在新会话中可用
(`hermes plugins enable` 后需重开会话让工具集生效)。

> ⚠️ **不要**给控制台写 systemd 单元 —— 用户明确要的是插件形态:
> 由 `hermes atbrains serve|stop` 管生命周期,不常驻、不注册服务。
