-- ============================================================
-- Agent 记忆库拆分迁移 (2026-10-12)
--   1) hermes: agent_memory → hermes_agent_memory（跨库 RENAME，元数据操作，无损）
--   2) 为 omp / dsh / artex 建独立库与账号（各自只有本库权限）
-- 执行: docker exec -i mariadb sh -c 'exec mariadb -u root -p"$MARIADB_ROOT_PASSWORD"' < migration-20261012.sql
-- ============================================================

CREATE DATABASE IF NOT EXISTS hermes_agent_memory CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

RENAME TABLE
  agent_memory.brain_memories TO hermes_agent_memory.brain_memories,
  agent_memory.brain_turns    TO hermes_agent_memory.brain_turns,
  agent_memory.brain_sessions TO hermes_agent_memory.brain_sessions,
  agent_memory.brain_archive  TO hermes_agent_memory.brain_archive;

GRANT ALL PRIVILEGES ON hermes_agent_memory.* TO 'agent'@'%';

CREATE DATABASE IF NOT EXISTS omp_agent_memory   CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS dsh_agent_memory   CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS artex_agent_memory CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'omp'@'%'   IDENTIFIED BY '***REDACTED***';
CREATE USER IF NOT EXISTS 'dsh'@'%'   IDENTIFIED BY '***REDACTED***';
CREATE USER IF NOT EXISTS 'artex'@'%' IDENTIFIED BY '***REDACTED***';

GRANT ALL PRIVILEGES ON omp_agent_memory.*   TO 'omp'@'%';
GRANT ALL PRIVILEGES ON dsh_agent_memory.*   TO 'dsh'@'%';
GRANT ALL PRIVILEGES ON artex_agent_memory.* TO 'artex'@'%';

FLUSH PRIVILEGES;
