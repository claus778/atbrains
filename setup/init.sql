-- Agent 记忆库: 非 hermes 账号初始化
-- 仅在【空卷首次初始化】时由 mariadb 官方镜像自动执行 (docker-entrypoint-initdb.d)。
-- hermes 的库与账号由容器环境变量创建; 布局台账见 /root/agent-db/credentials.txt
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
