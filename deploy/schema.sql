-- atbrains 记忆库结构 v12（由 tools/export-schema.py --from-code 生成）
SET NAMES utf8mb4;
CREATE DATABASE IF NOT EXISTS `agent_memory` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `agent_memory`;

-- ============================ 基础表 DDL ============================
CREATE TABLE IF NOT EXISTS brain_memories (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        kind VARCHAR(32) NOT NULL DEFAULT 'note',
        content MEDIUMTEXT NOT NULL,
        tags VARCHAR(255) NOT NULL DEFAULT '',
        importance TINYINT NOT NULL DEFAULT 5,
        source VARCHAR(64) NOT NULL DEFAULT '',
        session_id VARCHAR(96) NOT NULL DEFAULT '',
        machine VARCHAR(48) NOT NULL DEFAULT '',
        active TINYINT NOT NULL DEFAULT 1,
        deactivated_at DATETIME NULL DEFAULT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        content_hash CHAR(32) GENERATED ALWAYS AS (IF(active = 1, MD5(content), NULL)) STORED,
        PRIMARY KEY (id),
        KEY idx_active_updated (active, updated_at),
        KEY idx_kind (kind),
        KEY idx_recall (active, importance, updated_at),
        UNIQUE KEY uniq_active_content (content_hash),
        CONSTRAINT chk_importance CHECK (importance BETWEEN 1 AND 10),
        CONSTRAINT chk_active CHECK (active IN (0, 1))
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brain_turns (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        session_id VARCHAR(96) NOT NULL DEFAULT '',
        platform VARCHAR(48) NOT NULL DEFAULT '',
        author VARCHAR(64) NOT NULL DEFAULT '',
        machine VARCHAR(48) NOT NULL DEFAULT '',
        truncated TINYINT NOT NULL DEFAULT 0,
        user_content MEDIUMTEXT NOT NULL,
        assistant_content MEDIUMTEXT NOT NULL,
        ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        KEY idx_ts (ts),
        KEY idx_session_ts (session_id, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brain_turn_messages (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        turn_id BIGINT UNSIGNED NOT NULL,
        seq SMALLINT UNSIGNED NOT NULL DEFAULT 0,
        role VARCHAR(16) NOT NULL DEFAULT '',
        tool_name VARCHAR(64) NOT NULL DEFAULT '',
        tool_call_id VARCHAR(128) NOT NULL DEFAULT '',
        raw_bytes INT UNSIGNED NOT NULL DEFAULT 0,
        body LONGBLOB NOT NULL,
        ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        KEY idx_turn_seq (turn_id, seq),
        KEY idx_tm_ts (ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brain_thoughts (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        turn_id BIGINT UNSIGNED NOT NULL,
        seq SMALLINT UNSIGNED NOT NULL DEFAULT 0,
        message_uid VARCHAR(96) NOT NULL DEFAULT '',
        reasoning MEDIUMTEXT NOT NULL,
        reasoning_chars INT UNSIGNED NOT NULL DEFAULT 0,
        finish_reason VARCHAR(32) NOT NULL DEFAULT '',
        ts DATETIME NULL DEFAULT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uniq_thought (turn_id, seq),
        KEY idx_thought_ts (ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brain_tool_calls (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        turn_id BIGINT UNSIGNED NOT NULL,
        seq SMALLINT UNSIGNED NOT NULL DEFAULT 0,
        call_index SMALLINT UNSIGNED NOT NULL DEFAULT 0,
        call_id VARCHAR(128) NOT NULL DEFAULT '',
        tool_name VARCHAR(64) NOT NULL DEFAULT '',
        args MEDIUMTEXT NULL,
        result MEDIUMTEXT NULL,
        result_chars INT UNSIGNED NOT NULL DEFAULT 0,
        status VARCHAR(16) NOT NULL DEFAULT 'pending',
        started_at DATETIME NULL DEFAULT NULL,
        ended_at DATETIME NULL DEFAULT NULL,
        duration_ms INT UNSIGNED NULL DEFAULT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uniq_tool_call (turn_id, seq, call_index),
        KEY idx_tc_tool (tool_name),
        KEY idx_tc_status (status),
        KEY idx_tc_turn (turn_id, seq)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brain_sessions (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        session_id VARCHAR(96) NOT NULL,
        platform VARCHAR(48) NOT NULL DEFAULT '',
        machine VARCHAR(48) NOT NULL DEFAULT '',
        started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_active DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        turn_count INT NOT NULL DEFAULT 0,
        archived_messages INT UNSIGNED NOT NULL DEFAULT 0,
        note VARCHAR(512) NOT NULL DEFAULT '',
        PRIMARY KEY (id),
        UNIQUE KEY uniq_session_id (session_id),
        KEY idx_sessions_time (started_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brain_archive (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        session_id VARCHAR(96) NOT NULL DEFAULT '',
        machine VARCHAR(48) NOT NULL DEFAULT '',
        platform VARCHAR(48) NOT NULL DEFAULT '',
        kind VARCHAR(24) NOT NULL DEFAULT 'precompress',
        ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        payload MEDIUMTEXT NOT NULL,
        PRIMARY KEY (id),
        KEY idx_archive (session_id, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brain_meta (
        meta_key VARCHAR(64) NOT NULL,
        meta_value VARCHAR(255) NOT NULL DEFAULT '',
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (meta_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brain_vector_index (
        mem_id BIGINT UNSIGNED NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'ok',
        model VARCHAR(64) NOT NULL DEFAULT '',
        dim SMALLINT UNSIGNED NOT NULL DEFAULT 0,
        content_hash CHAR(32) NULL DEFAULT NULL,
        error VARCHAR(255) NOT NULL DEFAULT '',
        embedded_at DATETIME NULL DEFAULT NULL,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (mem_id),
        KEY idx_vec_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brain_claims (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        machine VARCHAR(48) NOT NULL DEFAULT '',
        subject VARCHAR(191) NOT NULL,
        predicate VARCHAR(96) NOT NULL,
        kind VARCHAR(24) NOT NULL DEFAULT 'fact',
        slot_key CHAR(32) NOT NULL,
        current_revision INT UNSIGNED NOT NULL DEFAULT 1,
        state VARCHAR(16) NOT NULL DEFAULT 'active',
        importance TINYINT NOT NULL DEFAULT 5,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        UNIQUE KEY uniq_claim_slot (slot_key),
        KEY idx_claim_subject (subject, predicate),
        KEY idx_claim_state (state, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brain_claim_versions (
        claim_id BIGINT UNSIGNED NOT NULL,
        revision INT UNSIGNED NOT NULL,
        value_text MEDIUMTEXT NOT NULL,
        basis VARCHAR(16) NOT NULL DEFAULT 'direct_report',
        valid_from DATETIME NULL DEFAULT NULL,
        valid_to DATETIME NULL DEFAULT NULL,
        recorded_from DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        recorded_to DATETIME NULL DEFAULT NULL,
        replaces_revision INT UNSIGNED NULL DEFAULT NULL,
        source_turn_id BIGINT UNSIGNED NULL DEFAULT NULL,
        source_seq SMALLINT UNSIGNED NULL DEFAULT NULL,
        quote VARCHAR(2048) NOT NULL DEFAULT '',
        confidence TINYINT UNSIGNED NOT NULL DEFAULT 80,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (claim_id, revision),
        KEY idx_cv_source (source_turn_id),
        KEY idx_cv_recorded (recorded_from)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brain_claim_evidence (
        claim_id BIGINT UNSIGNED NOT NULL,
        revision INT UNSIGNED NOT NULL,
        turn_id BIGINT UNSIGNED NOT NULL,
        seq SMALLINT UNSIGNED NOT NULL DEFAULT 0,
        relation VARCHAR(16) NOT NULL DEFAULT 'supports',
        quote VARCHAR(2048) NOT NULL DEFAULT '',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (claim_id, revision, turn_id, seq, relation),
        KEY idx_evidence_turn (turn_id, seq)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brain_episodes (
        turn_id BIGINT UNSIGNED NOT NULL,
        session_id VARCHAR(96) NOT NULL DEFAULT '',
        machine VARCHAR(48) NOT NULL DEFAULT '',
        goal TEXT NULL,
        decisions MEDIUMTEXT NULL,
        open_items MEDIUMTEXT NULL,
        blockers MEDIUMTEXT NULL,
        next_step TEXT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'open',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (turn_id),
        KEY idx_episode_session (session_id, turn_id),
        KEY idx_episode_status (status, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brain_author_defaults (
        machine VARCHAR(48) NOT NULL DEFAULT '*',
        platform VARCHAR(32) NOT NULL DEFAULT '*',
        author VARCHAR(64) NOT NULL,
        note VARCHAR(160) NOT NULL DEFAULT '',
        PRIMARY KEY (machine, platform)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================ 增量迁移 ============================
-- v1: machine 列（多设备归属）
ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS machine VARCHAR(48) NOT NULL DEFAULT '' AFTER session_id;
ALTER TABLE brain_turns ADD COLUMN IF NOT EXISTS machine VARCHAR(48) NOT NULL DEFAULT '' AFTER author;
ALTER TABLE brain_sessions ADD COLUMN IF NOT EXISTS machine VARCHAR(48) NOT NULL DEFAULT '' AFTER platform;

-- v2: 防重唯一键 / 召回索引 / 停用时间 / CHECK / 截断标记 / 存档来源列
ALTER TABLE brain_memories MODIFY COLUMN content MEDIUMTEXT NOT NULL;
ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS deactivated_at DATETIME NULL DEFAULT NULL AFTER active;
ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS content_hash CHAR(32) GENERATED ALWAYS AS (IF(active = 1, MD5(content), NULL)) STORED;
ALTER TABLE brain_memories ADD UNIQUE KEY IF NOT EXISTS uniq_active_content (content_hash);
ALTER TABLE brain_memories ADD INDEX IF NOT EXISTS idx_recall (active, importance, updated_at);
ALTER TABLE brain_memories ADD CONSTRAINT IF NOT EXISTS chk_importance CHECK (importance BETWEEN 1 AND 10);
ALTER TABLE brain_memories ADD CONSTRAINT IF NOT EXISTS chk_active CHECK (active IN (0, 1));
ALTER TABLE brain_turns ADD COLUMN IF NOT EXISTS truncated TINYINT NOT NULL DEFAULT 0 AFTER machine;
ALTER TABLE brain_archive ADD COLUMN IF NOT EXISTS machine VARCHAR(48) NOT NULL DEFAULT '' AFTER session_id;
ALTER TABLE brain_archive ADD COLUMN IF NOT EXISTS platform VARCHAR(48) NOT NULL DEFAULT '' AFTER machine;

-- v3: 向量索引表（brain_vector_index）
CREATE TABLE IF NOT EXISTS brain_vector_index (mem_id BIGINT UNSIGNED NOT NULL, status VARCHAR(16) NOT NULL DEFAULT 'ok', model VARCHAR(64) NOT NULL DEFAULT '', dim SMALLINT UNSIGNED NOT NULL DEFAULT 0, content_hash CHAR(32) NULL DEFAULT NULL, error VARCHAR(255) NOT NULL DEFAULT '', embedded_at DATETIME NULL DEFAULT NULL, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, PRIMARY KEY (mem_id), KEY idx_vec_status (status)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- v4: 取代关系与记忆分层
ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS superseded_by BIGINT UNSIGNED NULL DEFAULT NULL AFTER active;
ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS superseded_at DATETIME NULL DEFAULT NULL AFTER superseded_by;
ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS memory_type VARCHAR(16) NOT NULL DEFAULT '' AFTER kind;
ALTER TABLE brain_memories ADD INDEX IF NOT EXISTS idx_supersede (active, superseded_by);
UPDATE brain_memories SET memory_type = CASE WHEN kind = 'builtin-memory' THEN 'profile' WHEN kind IN ('fact', 'decision') THEN 'knowledge' ELSE 'episodic' END, updated_at = updated_at WHERE memory_type = '';

-- v5: 全量转录层（brain_turn_messages + 每轮计数 + 会话增量游标）
CREATE TABLE IF NOT EXISTS brain_turn_messages (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, turn_id BIGINT UNSIGNED NOT NULL, seq SMALLINT UNSIGNED NOT NULL DEFAULT 0, role VARCHAR(16) NOT NULL DEFAULT '', tool_name VARCHAR(64) NOT NULL DEFAULT '', tool_call_id VARCHAR(128) NOT NULL DEFAULT '', raw_bytes INT UNSIGNED NOT NULL DEFAULT 0, body LONGBLOB NOT NULL, ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (id), KEY idx_turn_seq (turn_id, seq), KEY idx_tm_ts (ts)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
ALTER TABLE brain_turns ADD COLUMN IF NOT EXISTS message_count SMALLINT UNSIGNED NOT NULL DEFAULT 0 AFTER truncated;
ALTER TABLE brain_turns ADD COLUMN IF NOT EXISTS message_bytes INT UNSIGNED NOT NULL DEFAULT 0 AFTER message_count;
ALTER TABLE brain_sessions ADD COLUMN IF NOT EXISTS archived_messages INT UNSIGNED NOT NULL DEFAULT 0 AFTER turn_count;

-- v6: 知识层：主张 / 版本 / 证据 / 情节
CREATE TABLE IF NOT EXISTS brain_claims (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, machine VARCHAR(48) NOT NULL DEFAULT '', subject VARCHAR(191) NOT NULL, predicate VARCHAR(96) NOT NULL, kind VARCHAR(24) NOT NULL DEFAULT 'fact', slot_key CHAR(32) NOT NULL, current_revision INT UNSIGNED NOT NULL DEFAULT 1, state VARCHAR(16) NOT NULL DEFAULT 'active', importance TINYINT NOT NULL DEFAULT 5, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, PRIMARY KEY (id), UNIQUE KEY uniq_claim_slot (slot_key), KEY idx_claim_subject (subject, predicate), KEY idx_claim_state (state, updated_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS brain_claim_versions (claim_id BIGINT UNSIGNED NOT NULL, revision INT UNSIGNED NOT NULL, value_text MEDIUMTEXT NOT NULL, basis VARCHAR(16) NOT NULL DEFAULT 'direct_report', valid_from DATETIME NULL DEFAULT NULL, valid_to DATETIME NULL DEFAULT NULL, recorded_from DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, recorded_to DATETIME NULL DEFAULT NULL, replaces_revision INT UNSIGNED NULL DEFAULT NULL, source_turn_id BIGINT UNSIGNED NULL DEFAULT NULL, source_seq SMALLINT UNSIGNED NULL DEFAULT NULL, quote VARCHAR(2048) NOT NULL DEFAULT '', confidence TINYINT UNSIGNED NOT NULL DEFAULT 80, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (claim_id, revision), KEY idx_cv_source (source_turn_id), KEY idx_cv_recorded (recorded_from)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS brain_claim_evidence (claim_id BIGINT UNSIGNED NOT NULL, revision INT UNSIGNED NOT NULL, turn_id BIGINT UNSIGNED NOT NULL, seq SMALLINT UNSIGNED NOT NULL DEFAULT 0, relation VARCHAR(16) NOT NULL DEFAULT 'supports', quote VARCHAR(2048) NOT NULL DEFAULT '', created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (claim_id, revision, turn_id, seq, relation), KEY idx_evidence_turn (turn_id, seq)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS brain_episodes (turn_id BIGINT UNSIGNED NOT NULL, session_id VARCHAR(96) NOT NULL DEFAULT '', machine VARCHAR(48) NOT NULL DEFAULT '', goal TEXT NULL, decisions MEDIUMTEXT NULL, open_items MEDIUMTEXT NULL, blockers MEDIUMTEXT NULL, next_step TEXT NULL, status VARCHAR(16) NOT NULL DEFAULT 'open', created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, PRIMARY KEY (turn_id), KEY idx_episode_session (session_id, turn_id), KEY idx_episode_status (status, updated_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- v7: agent 列（跨 agent 共享归属，与 machine 主机名解耦）
ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS agent VARCHAR(32) NOT NULL DEFAULT 'hermes' AFTER machine;
ALTER TABLE brain_claims ADD COLUMN IF NOT EXISTS agent VARCHAR(32) NOT NULL DEFAULT 'hermes' AFTER machine;
ALTER TABLE brain_episodes ADD COLUMN IF NOT EXISTS agent VARCHAR(32) NOT NULL DEFAULT 'hermes' AFTER machine;
ALTER TABLE brain_memories ADD INDEX IF NOT EXISTS idx_agent (active, agent, updated_at);
UPDATE brain_memories SET agent = machine, machine = 'server', updated_at = updated_at WHERE source = 'mcp' AND machine IN ('omp', 'dsh', 'artex', 'codex');

-- v8: 主张版本级 agent（历史版本各自记录写入方）
ALTER TABLE brain_claim_versions ADD COLUMN IF NOT EXISTS agent VARCHAR(32) NOT NULL DEFAULT 'hermes' AFTER revision;
UPDATE brain_claim_versions v JOIN brain_claims c ON c.id = v.claim_id SET v.agent = c.agent WHERE v.agent = 'hermes' AND c.agent <> 'hermes';

-- v9: 思考链与工具调用明细（brain_thoughts / brain_tool_calls）
CREATE TABLE IF NOT EXISTS brain_thoughts (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, turn_id BIGINT UNSIGNED NOT NULL, seq SMALLINT UNSIGNED NOT NULL DEFAULT 0, message_uid VARCHAR(96) NOT NULL DEFAULT '', reasoning MEDIUMTEXT NOT NULL, reasoning_chars INT UNSIGNED NOT NULL DEFAULT 0, finish_reason VARCHAR(32) NOT NULL DEFAULT '', ts DATETIME NULL DEFAULT NULL, PRIMARY KEY (id), UNIQUE KEY uniq_thought (turn_id, seq), KEY idx_thought_ts (ts)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS brain_tool_calls (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, turn_id BIGINT UNSIGNED NOT NULL, seq SMALLINT UNSIGNED NOT NULL DEFAULT 0, call_index SMALLINT UNSIGNED NOT NULL DEFAULT 0, call_id VARCHAR(128) NOT NULL DEFAULT '', tool_name VARCHAR(64) NOT NULL DEFAULT '', args MEDIUMTEXT NULL, result MEDIUMTEXT NULL, result_chars INT UNSIGNED NOT NULL DEFAULT 0, status VARCHAR(16) NOT NULL DEFAULT 'pending', started_at DATETIME NULL DEFAULT NULL, ended_at DATETIME NULL DEFAULT NULL, duration_ms INT UNSIGNED NULL DEFAULT NULL, PRIMARY KEY (id), UNIQUE KEY uniq_tool_call (turn_id, seq, call_index), KEY idx_tc_tool (tool_name), KEY idx_tc_status (status), KEY idx_tc_turn (turn_id, seq)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- v10: 访问反馈 / 按类型 TTL（借 LivingMemory 的 access_count 与原子 TTL）
ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS access_count INT UNSIGNED NOT NULL DEFAULT 0 AFTER importance;
ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS last_accessed_at DATETIME NULL DEFAULT NULL AFTER access_count;
ALTER TABLE brain_memories ADD COLUMN IF NOT EXISTS expires_at DATETIME NULL DEFAULT NULL AFTER memory_type;
ALTER TABLE brain_memories ADD INDEX IF NOT EXISTS idx_access (active, access_count);
ALTER TABLE brain_memories ADD INDEX IF NOT EXISTS idx_expires (active, expires_at);

-- v11: 图谱层：实体 / 关系 / 提及（第三路召回）
CREATE TABLE IF NOT EXISTS brain_entities (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, name VARCHAR(191) NOT NULL, kind VARCHAR(32) NOT NULL DEFAULT '', machine VARCHAR(48) NOT NULL DEFAULT '', agent VARCHAR(32) NOT NULL DEFAULT 'hermes', mention_count INT UNSIGNED NOT NULL DEFAULT 1, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, PRIMARY KEY (id), UNIQUE KEY uniq_entity_name (name), KEY idx_entity_kind (kind)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS brain_edges (id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, src_id BIGINT UNSIGNED NOT NULL, dst_id BIGINT UNSIGNED NOT NULL, relation VARCHAR(48) NOT NULL DEFAULT 'related', weight DECIMAL(6,3) NOT NULL DEFAULT 1.000, evidence_memory_id BIGINT UNSIGNED NULL DEFAULT NULL, machine VARCHAR(48) NOT NULL DEFAULT '', agent VARCHAR(32) NOT NULL DEFAULT 'hermes', created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (id), UNIQUE KEY uniq_edge (src_id, dst_id, relation), KEY idx_edge_src (src_id), KEY idx_edge_dst (dst_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS brain_entity_mentions (entity_id BIGINT UNSIGNED NOT NULL, memory_id BIGINT UNSIGNED NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (entity_id, memory_id), KEY idx_em_memory (memory_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- v12: 通用化收口：补全 brain_author_defaults 与 brain_turns.agent 列
CREATE TABLE IF NOT EXISTS brain_author_defaults (machine VARCHAR(48) NOT NULL DEFAULT '*', platform VARCHAR(32) NOT NULL DEFAULT '*', author VARCHAR(64) NOT NULL, note VARCHAR(160) NOT NULL DEFAULT '', PRIMARY KEY (machine, platform)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
ALTER TABLE brain_turns ADD COLUMN IF NOT EXISTS agent VARCHAR(32) NOT NULL DEFAULT '' AFTER machine;

INSERT INTO brain_meta (meta_key, meta_value) VALUES ('schema_version', '12') ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value);
