-- atbrains 又一层 bootstrap：只负责「库 + 账号 + 时区口径」，不建表。
-- 表结构由 setup/schema-v9.sql 或插件 ensure_schema() 建（14 表 / 10 视图 / 10 触发器）。
--
-- 说明：用 mariadb 官方镜像时，库和账号其实已经由容器环境变量建好了
--   MARIADB_DATABASE=agent_memory / MARIADB_USER=agent / MARIADB_PASSWORD=...
-- 本文件只在两种情况下需要：
--   1) 你用别的镜像/已有 MariaDB，需要手工建库建号；
--   2) 想额外建一个只读账号给面板/审计用（见文件末尾，默认注释）。
--
-- ⚠️ 2026-10-24 架构变更（schema v8）：不再按 agent 分库。
--    旧的 omp_agent_memory / dsh_agent_memory / artex_agent_memory 三库与同名账号已删除，
--    所有 agent 共用 agent_memory.brain_memories，靠 agent 列区分写入方。
--    历史文件见 init.sql.old-20261025，仅作考古。

CREATE DATABASE IF NOT EXISTS `agent_memory`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'agent'@'%' IDENTIFIED BY '<MARIADB_PASSWORD>';
GRANT ALL PRIVILEGES ON `agent_memory`.* TO 'agent'@'%';
FLUSH PRIVILEGES;

-- 库内时间统一 UTC（容器 default-time-zone 已由 99-timezone.cnf 固定为 +00:00；
-- 这里再确认一次会话级口径，避免别人的客户端按本地时区写入）。
SET GLOBAL time_zone = '+00:00';

-- 可选：只读账号（面板/审计用；不需要就保持注释）
-- CREATE USER IF NOT EXISTS 'agent_ro'@'%' IDENTIFIED BY '<READONLY_PASSWORD>';
-- GRANT SELECT ON `agent_memory`.* TO 'agent_ro'@'%';
