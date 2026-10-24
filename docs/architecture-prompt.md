# atbrains 架构图提示词(2026-10-15 当前态)

> 用途:交给 drawio MCP 作图。所有节点名称/连线标签**照抄**,别改写。
> 状态标记:〔已启用〕〔默认关闭〕—— 作图时用不同边框/颜色区分。

## 一、画布与布局

- 横向布局,宽∶高 ≈ 4∶3。分**四层**(上→下)加**右侧一个云 API**。
- 统一字体无衬线;中文标签为主。
- 配色:客户端层 #EFF6FF(浅蓝)、服务器层 #F0FDF4(浅绿)、接入层 #FEF9C3(浅黄)、
  存储层 #F5F3FF(浅紫)、外部 API #FEE2E2(浅红)。
- 三种线型:实线=本机调用;蓝色虚线=公网直连;灰色虚线=**默认关闭的可选链路 / 进程内循环**。
- 本系统**不含任何 systemd 单元**(定时器已于 2026-10-17 退役) —— 若画到「系统服务」一类的东西,
  只可能有 `hermes-gateway`(Hermes 自身宿主)与 `docker`(存储容器守护),它们不属于记忆系统。

## 二、节点清单(照抄文字)

### 层 1 · 客户端 / 各机器(一个大虚线框)

框标题:`客户端 / 各机器 —— 均装 brain 插件 —— 长期层全机共享 · 短期层按库隔离`

内部 5 个方框(每个框内两行):

| 框名 | 第二行 |
|---|---|
| `Hermes · server(本机)` | `brain 插件 · Redis db0` |
| `Hermes · x260-debian` | `brain 插件 · Redis db1` |
| `Hermes · r9000p-Windows` | `brain 插件 · Redis db2` |
| `Hermes · gigabyte-debian` | `brain 插件 · Redis db3` |
| `omp / dsh(独立 Agent,经 MCP)` | `共享 hermes_agent_memory · machine 标注 omp/dsh` |
| `artex(独立 Agent)` | `独立库 artex_agent_memory · Redis db7` |

> 前 4 个是同一套 Hermes 的 4 个实例;第 5 个是非 Hermes 的独立 agent。
> 在 server 框上贴一个小标签:`本机直连,不走远程`。

### 层 2 · 腾讯云服务器(Debian 13)大框

框标题:`腾讯云服务器(Debian 13)· Docker 容器 · 数据绑定挂载在 /root`

内部再分两个子区:

**子区 A —— 接入 / 插件层**(黄)

1. `atbrains 插件 v1.2.0 —— 监控 + Memory Service + MCP`
   内部四个条目:
   - `Memory Service /v1 〔已启用〕`
     `recall / remember / forget / recent / turn / status`
   - `MCP 端点 /mcp 〔已启用〕`
     `10 个 brain_* 工具 · JSON-RPC 2.0 · Bearer 令牌`
   - `监控控制台 :8787`
     `6 面板 · token 鉴权`
   - `令牌表:服务端反查 machine(不可伪造)`
2. `brain 插件 —— 检索与存储核心`
   内部按管线顺序列(用 → 连接):
   - `关键词检索 + 向量检索(2560 维)`
   - `RRF 融合 k=60`
   - `时间衰减:profile/knowledge 不衰减 · episodic 半衰期 45 天 〔已启用〕`
   - `取代过滤:superseded_by IS NULL 〔已启用〕`
   - `Rerank:Cross-Encoder 〔默认关闭〕`
   - `注入上限 1200 字 · 召回 5 条记忆 + 3 轮对话`
   - `相似度下限 vector_min_score = 0.25`
   在框内左下角贴:`memory_type 三分层:schema v4`

**子区 B —— 进程内自动化(零 systemd)**(虚线小框)

- `进程内巡检 — 长命进程每 60 秒补缺失向量`
- `brain-maintenance 线程 — 距上次清理超 7 天才执行`
- 小字:`不依赖任何 systemd 单元`

### 层 3 · 存储层(三个圆柱体,横排)

| 标题 | 内容行 |
|---|---|
| `MariaDB :57306` | `唯一权威 · 永久 · 全机共享`<br>`库 hermes_agent_memory`<br>`brain_memories / brain_turns / brain_sessions / brain_archive / brain_vector_index / brain_meta`<br>`schema v4 · utf8mb4 · 时区 UTC`<br>`新增列:memory_type / superseded_by / superseded_at` |
| `Redis :9079` | `短期 · 全部键带 TTL`<br>`db0 server · db1 x260-debian · db2 r9000p-Windows`<br>`db3 gigabyte-debian · db4 预留 · db5-7 omp/dsh/artex`<br>`brain:recent 对话流 · brain:hot:* 热点镜像 · qcache / qemb 缓存`<br>`可选:key 前缀隔离(替代多 DB)〔默认关闭〕` |
| `Qdrant :6333 / :6334` | `向量语义层 · 可随时重建`<br>`集合 brain_memories`<br>`2560 维 · Cosine`<br>`访问口令仅在配置文件(只读挂载)` |

### 右侧 · 外部云 API(红框)

`SiliconFlow 云 API`(HTTPS 公网依赖)
- `Qwen/Qwen3-Embedding-4B → 2560 维向量`〔已启用〕
- `BAAI/bge-reranker-v2-m3 重排`〔默认关闭〕
- 小字:`api.siliconflow.cn`

## 三、连线(源 → 目标 · 标签 · 线型)

1. 客户端 brain 插件 → **Memory Service /v1** · `远程模式 HTTP recall/remember · Bearer 令牌`
   · 蓝色虚线 ·〔默认关闭〕。旁边注:`失败自动回落本地直连(33ms)`
2. 客户端 brain 插件 → **存储层(MariaDB + Redis)** · `本机直连 · SQL / RESP`
   · 蓝色虚线 ·〔已启用〕(这是当前默认路径)
3. **brain 插件核心** → **存储层** · `读写记忆 / 归档对话` · 实线
4. **brain 插件核心** ↔ **Qdrant** · `向量读写` · 实线
5. **brain 插件核心** ↔ **SiliconFlow** · `embed(2560 维) · rerank(可选)` · 蓝色虚线双向
6. **进程内巡检循环** → **brain 插件核心** · `长命进程每 60 秒补缺失向量` · 灰色细虚线(它是插件自身的线程,可画成插件内部的环形箭头)
7. **atbrains 插件** → **存储层** · `监控三读 + 手动回填` · 实线
8. **监控控制台** ↔ 用户浏览器 · `:8787 · token 鉴权` · 细实线(可省)
9. **omp / dsh** → **MCP 端点 /mcp** → **brain 插件核心** · `JSON-RPC · Bearer 令牌` · 蓝色虚线(公网/本机)
   旁注:`共享库 · 写入按令牌标注 agent`

## 四、数据流图例(右下角小框)

标题:`数据流`

- `① 召回:关键词 + 向量 → RRF → 时间衰减 → 取代过滤 → [rerank] → 注入上限 1200 字`
- `② 写记忆:brain_remember → MariaDB(权威) → Redis 热点 → 异步向量化 → Qdrant`
- `③ 归档:每轮对话 → brain_turns + brain:recent;压缩前全文 → brain_archive`
- `④ 向量化兜底:drain.timer 每 3 分钟补漏(SiliconFlow 尾延迟 20-60s 的补偿)`
- `⑤ 取代:同层相似度 ≥0.90 时较新者胜,旧条 superseded_by 标记,检索侧自动过滤`
- `⑥ 远程模式:客户端 HTTP 调 /v1,服务端按令牌认机器;失败回落本地`
- `⑦ 监控运维:atbrains 三头读取 + `hermes atbrains status`
- `⑧ 多 agent:omp/dsh → MCP /mcp(令牌认 agent)→ 同一 brain 核心 → 共享 MariaDB`

线例:实线 = 本机调用 · 蓝虚线 = 公网 · 灰虚线 = 默认关闭

## 五、底部注记(两行)

`图注:记忆原件只保存在 MariaDB(唯一权威);Redis 到期自清、Qdrant 可随时重建 —— 二者故障均不丢数据。`

`短期层按机器 / Agent 分库隔离(db0-7),长期层全机共享、可跨设备召回;跨机只同步记忆,不同步对话记录。`

## 六、状态一览(可做成角落小标签)

| 能力 | 状态 |
|---|---|
| schema v4(三分层 + 取代标记) | 已启用 |
| 时间衰减(episodic 半衰期 45 天) | 已启用 |
| 取代过滤 | 已启用(库内 2 组已应用) |
| Memory Service /v1 | 已启用(客户端尚未切换) |
| 客户端远程模式 | 默认关闭 |
| Rerank | 默认关闭(A/B 实测未通过) |
| Redis key 前缀 | 默认关闭 |
| 进程内自动化(零 systemd) | 已启用(定时器已退役) |
| 冷备 / DR | 未做(用户暂缓) |
