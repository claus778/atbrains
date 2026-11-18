-- atbrains 记忆库结构 v9 —— 表 / 视图 / 触发器
-- 自动导出（tools/export-schema.py），导出时间 2026-10-25 11:16:47Z
-- 不含数据、不含 AUTO_INCREMENT 计数、已剥离 DEFINER（任何账号都能导入）
-- 用法：先建库，再导入本文件 ——
--   CREATE DATABASE agent_memory CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
--   mariadb -h127.0.0.1 -P57306 -uagent -p agent_memory < schema-v9.sql
-- 注：插件 ensure_schema() 也会幂等建出同样的结构；本文件用于审阅/离线重建。

SET NAMES utf8mb4;
USE `agent_memory`;

-- ============================ 表 ============================

-- ---- brain_archive ----
CREATE TABLE IF NOT EXISTS `brain_archive` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `session_id` varchar(96) NOT NULL DEFAULT '',
  `machine` varchar(48) NOT NULL DEFAULT '',
  `platform` varchar(48) NOT NULL DEFAULT '',
  `kind` varchar(24) NOT NULL DEFAULT 'precompress',
  `ts` datetime NOT NULL DEFAULT current_timestamp(),
  `payload` mediumtext NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_archive` (`session_id`,`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- brain_author_defaults ----
CREATE TABLE IF NOT EXISTS `brain_author_defaults` (
  `machine` varchar(48) NOT NULL DEFAULT '*',
  `platform` varchar(32) NOT NULL DEFAULT '*',
  `author` varchar(64) NOT NULL,
  `note` varchar(160) NOT NULL DEFAULT '',
  PRIMARY KEY (`machine`,`platform`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- brain_claims ----
CREATE TABLE IF NOT EXISTS `brain_claims` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `machine` varchar(48) NOT NULL DEFAULT '',
  `agent` varchar(32) NOT NULL DEFAULT 'hermes',
  `subject` varchar(191) NOT NULL,
  `predicate` varchar(96) NOT NULL,
  `kind` varchar(24) NOT NULL DEFAULT 'fact',
  `slot_key` char(32) NOT NULL,
  `current_revision` int(10) unsigned NOT NULL DEFAULT 1,
  `state` varchar(16) NOT NULL DEFAULT 'active',
  `importance` tinyint(4) NOT NULL DEFAULT 5,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_claim_slot` (`slot_key`),
  KEY `idx_claim_subject` (`subject`,`predicate`),
  KEY `idx_claim_state` (`state`,`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- brain_claim_evidence ----
CREATE TABLE IF NOT EXISTS `brain_claim_evidence` (
  `claim_id` bigint(20) unsigned NOT NULL,
  `revision` int(10) unsigned NOT NULL,
  `turn_id` bigint(20) unsigned NOT NULL,
  `seq` smallint(5) unsigned NOT NULL DEFAULT 0,
  `relation` varchar(16) NOT NULL DEFAULT 'supports',
  `quote` varchar(2048) NOT NULL DEFAULT '',
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`claim_id`,`revision`,`turn_id`,`seq`,`relation`),
  KEY `idx_evidence_turn` (`turn_id`,`seq`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- brain_claim_versions ----
CREATE TABLE IF NOT EXISTS `brain_claim_versions` (
  `claim_id` bigint(20) unsigned NOT NULL,
  `revision` int(10) unsigned NOT NULL,
  `agent` varchar(32) NOT NULL DEFAULT 'hermes',
  `value_text` mediumtext NOT NULL,
  `basis` varchar(16) NOT NULL DEFAULT 'direct_report',
  `valid_from` datetime DEFAULT NULL,
  `valid_to` datetime DEFAULT NULL,
  `recorded_from` datetime NOT NULL DEFAULT current_timestamp(),
  `recorded_to` datetime DEFAULT NULL,
  `replaces_revision` int(10) unsigned DEFAULT NULL,
  `source_turn_id` bigint(20) unsigned DEFAULT NULL,
  `source_seq` smallint(5) unsigned DEFAULT NULL,
  `quote` varchar(2048) NOT NULL DEFAULT '',
  `confidence` tinyint(3) unsigned NOT NULL DEFAULT 80,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`claim_id`,`revision`),
  KEY `idx_cv_source` (`source_turn_id`),
  KEY `idx_cv_recorded` (`recorded_from`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- brain_episodes ----
CREATE TABLE IF NOT EXISTS `brain_episodes` (
  `turn_id` bigint(20) unsigned NOT NULL,
  `session_id` varchar(96) NOT NULL DEFAULT '',
  `machine` varchar(48) NOT NULL DEFAULT '',
  `agent` varchar(32) NOT NULL DEFAULT 'hermes',
  `goal` text DEFAULT NULL,
  `decisions` mediumtext DEFAULT NULL,
  `open_items` mediumtext DEFAULT NULL,
  `blockers` mediumtext DEFAULT NULL,
  `next_step` text DEFAULT NULL,
  `status` varchar(16) NOT NULL DEFAULT 'open',
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`turn_id`),
  KEY `idx_episode_session` (`session_id`,`turn_id`),
  KEY `idx_episode_status` (`status`,`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- brain_memories ----
CREATE TABLE IF NOT EXISTS `brain_memories` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `kind` varchar(32) NOT NULL DEFAULT 'note',
  `memory_type` varchar(16) NOT NULL DEFAULT '',
  `content` mediumtext NOT NULL,
  `tags` varchar(255) NOT NULL DEFAULT '',
  `importance` tinyint(4) NOT NULL DEFAULT 5,
  `source` varchar(64) NOT NULL DEFAULT '',
  `session_id` varchar(96) NOT NULL DEFAULT '',
  `machine` varchar(48) NOT NULL DEFAULT '',
  `agent` varchar(32) NOT NULL DEFAULT 'hermes',
  `active` tinyint(4) NOT NULL DEFAULT 1,
  `superseded_by` bigint(20) unsigned DEFAULT NULL,
  `superseded_at` datetime DEFAULT NULL,
  `deactivated_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  `content_hash` char(32) GENERATED ALWAYS AS (if(`active` = 1,md5(`content`),NULL)) STORED,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_active_content` (`content_hash`),
  KEY `idx_active_updated` (`active`,`updated_at`),
  KEY `idx_kind` (`kind`),
  KEY `idx_recall` (`active`,`importance`,`updated_at`),
  KEY `idx_supersede` (`active`,`superseded_by`),
  KEY `idx_agent` (`active`,`agent`,`updated_at`),
  CONSTRAINT `chk_importance` CHECK (`importance` between 1 and 10),
  CONSTRAINT `chk_active` CHECK (`active` in (0,1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- brain_meta ----
CREATE TABLE IF NOT EXISTS `brain_meta` (
  `meta_key` varchar(64) NOT NULL,
  `meta_value` varchar(255) NOT NULL DEFAULT '',
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`meta_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- brain_sessions ----
CREATE TABLE IF NOT EXISTS `brain_sessions` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `session_id` varchar(96) NOT NULL,
  `platform` varchar(48) NOT NULL DEFAULT '',
  `machine` varchar(48) NOT NULL DEFAULT '',
  `started_at` datetime NOT NULL DEFAULT current_timestamp(),
  `last_active` datetime NOT NULL DEFAULT current_timestamp(),
  `turn_count` int(11) NOT NULL DEFAULT 0,
  `archived_messages` int(10) unsigned NOT NULL DEFAULT 0,
  `note` varchar(512) NOT NULL DEFAULT '',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_session_id` (`session_id`),
  KEY `idx_sessions_time` (`started_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- brain_thoughts ----
CREATE TABLE IF NOT EXISTS `brain_thoughts` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `turn_id` bigint(20) unsigned NOT NULL,
  `seq` smallint(5) unsigned NOT NULL DEFAULT 0,
  `message_uid` varchar(96) NOT NULL DEFAULT '',
  `reasoning` mediumtext NOT NULL,
  `reasoning_chars` int(10) unsigned NOT NULL DEFAULT 0,
  `finish_reason` varchar(32) NOT NULL DEFAULT '',
  `ts` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_thought` (`turn_id`,`seq`),
  KEY `idx_thought_ts` (`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- brain_tool_calls ----
CREATE TABLE IF NOT EXISTS `brain_tool_calls` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `turn_id` bigint(20) unsigned NOT NULL,
  `seq` smallint(5) unsigned NOT NULL DEFAULT 0,
  `call_index` smallint(5) unsigned NOT NULL DEFAULT 0,
  `call_id` varchar(128) NOT NULL DEFAULT '',
  `tool_name` varchar(64) NOT NULL DEFAULT '',
  `args` mediumtext DEFAULT NULL,
  `result` mediumtext DEFAULT NULL,
  `result_chars` int(10) unsigned NOT NULL DEFAULT 0,
  `status` varchar(16) NOT NULL DEFAULT 'pending',
  `started_at` datetime DEFAULT NULL,
  `ended_at` datetime DEFAULT NULL,
  `duration_ms` int(10) unsigned DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_tool_call` (`turn_id`,`seq`,`call_index`),
  KEY `idx_tc_tool` (`tool_name`),
  KEY `idx_tc_status` (`status`),
  KEY `idx_tc_turn` (`turn_id`,`seq`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- brain_turns ----
CREATE TABLE IF NOT EXISTS `brain_turns` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `session_id` varchar(96) NOT NULL DEFAULT '',
  `platform` varchar(48) NOT NULL DEFAULT '',
  `author` varchar(64) NOT NULL DEFAULT '',
  `machine` varchar(48) NOT NULL DEFAULT '',
  `truncated` tinyint(4) NOT NULL DEFAULT 0,
  `message_count` smallint(5) unsigned NOT NULL DEFAULT 0,
  `message_bytes` int(10) unsigned NOT NULL DEFAULT 0,
  `user_content` mediumtext NOT NULL,
  `assistant_content` mediumtext NOT NULL,
  `ts` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_ts` (`ts`),
  KEY `idx_session_ts` (`session_id`,`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- brain_turn_messages ----
CREATE TABLE IF NOT EXISTS `brain_turn_messages` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `turn_id` bigint(20) unsigned NOT NULL,
  `seq` smallint(5) unsigned NOT NULL DEFAULT 0,
  `role` varchar(16) NOT NULL DEFAULT '',
  `tool_name` varchar(64) NOT NULL DEFAULT '',
  `tool_call_id` varchar(128) NOT NULL DEFAULT '',
  `raw_bytes` int(10) unsigned NOT NULL DEFAULT 0,
  `body` longblob NOT NULL,
  `ts` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_turn_seq` (`turn_id`,`seq`),
  KEY `idx_tm_ts` (`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- brain_vector_index ----
CREATE TABLE IF NOT EXISTS `brain_vector_index` (
  `mem_id` bigint(20) unsigned NOT NULL,
  `status` varchar(16) NOT NULL DEFAULT 'ok',
  `model` varchar(64) NOT NULL DEFAULT '',
  `dim` smallint(5) unsigned NOT NULL DEFAULT 0,
  `content_hash` char(32) DEFAULT NULL,
  `error` varchar(255) NOT NULL DEFAULT '',
  `embedded_at` datetime DEFAULT NULL,
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`mem_id`),
  KEY `idx_vec_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================ 视图 ============================

-- ---- brain_archive_bj ----
DROP VIEW IF EXISTS `brain_archive_bj`;
CREATE OR REPLACE ALGORITHM=UNDEFINED SQL SECURITY INVOKER VIEW `brain_archive_bj` AS select `brain_archive`.`id` AS `id`,`brain_archive`.`session_id` AS `session_id`,`brain_archive`.`machine` AS `machine`,`brain_archive`.`platform` AS `platform`,`brain_archive`.`kind` AS `kind`,`brain_archive`.`ts` + interval 8 hour AS `ts`,`brain_archive`.`payload` AS `payload` from `brain_archive`;

-- ---- brain_memories_bj ----
DROP VIEW IF EXISTS `brain_memories_bj`;
CREATE OR REPLACE ALGORITHM=UNDEFINED SQL SECURITY INVOKER VIEW `brain_memories_bj` AS select `brain_memories`.`id` AS `id`,`brain_memories`.`kind` AS `kind`,`brain_memories`.`memory_type` AS `memory_type`,`brain_memories`.`content` AS `content`,`brain_memories`.`tags` AS `tags`,`brain_memories`.`importance` AS `importance`,`brain_memories`.`source` AS `source`,`brain_memories`.`session_id` AS `session_id`,`brain_memories`.`machine` AS `machine`,`brain_memories`.`agent` AS `agent`,`brain_memories`.`active` AS `active`,`brain_memories`.`superseded_by` AS `superseded_by`,`brain_memories`.`superseded_at` + interval 8 hour AS `superseded_at`,`brain_memories`.`deactivated_at` + interval 8 hour AS `deactivated_at`,`brain_memories`.`created_at` + interval 8 hour AS `created_at`,`brain_memories`.`updated_at` + interval 8 hour AS `updated_at`,`brain_memories`.`content_hash` AS `content_hash` from `brain_memories`;

-- ---- brain_memories_newest ----
DROP VIEW IF EXISTS `brain_memories_newest`;
CREATE OR REPLACE ALGORITHM=UNDEFINED SQL SECURITY INVOKER VIEW `brain_memories_newest` AS select `brain_memories`.`id` AS `id`,`brain_memories`.`kind` AS `kind`,`brain_memories`.`content` AS `content`,`brain_memories`.`tags` AS `tags`,`brain_memories`.`importance` AS `importance`,`brain_memories`.`source` AS `source`,`brain_memories`.`session_id` AS `session_id`,`brain_memories`.`machine` AS `machine`,`brain_memories`.`active` AS `active`,`brain_memories`.`created_at` AS `created_at`,`brain_memories`.`updated_at` AS `updated_at` from `brain_memories` order by `brain_memories`.`created_at` desc,`brain_memories`.`id` desc;

-- ---- brain_sessions_bj ----
DROP VIEW IF EXISTS `brain_sessions_bj`;
CREATE OR REPLACE ALGORITHM=UNDEFINED SQL SECURITY INVOKER VIEW `brain_sessions_bj` AS select `brain_sessions`.`id` AS `id`,`brain_sessions`.`session_id` AS `session_id`,`brain_sessions`.`platform` AS `platform`,`brain_sessions`.`machine` AS `machine`,`brain_sessions`.`started_at` + interval 8 hour AS `started_at`,`brain_sessions`.`last_active` + interval 8 hour AS `last_active`,`brain_sessions`.`turn_count` AS `turn_count`,`brain_sessions`.`archived_messages` AS `archived_messages`,`brain_sessions`.`note` AS `note` from `brain_sessions`;

-- ---- brain_sessions_newest ----
DROP VIEW IF EXISTS `brain_sessions_newest`;
CREATE OR REPLACE ALGORITHM=UNDEFINED SQL SECURITY INVOKER VIEW `brain_sessions_newest` AS select `brain_sessions`.`session_id` AS `session_id`,`brain_sessions`.`platform` AS `platform`,`brain_sessions`.`machine` AS `machine`,`brain_sessions`.`started_at` AS `started_at`,`brain_sessions`.`last_active` AS `last_active`,`brain_sessions`.`turn_count` AS `turn_count` from `brain_sessions` order by `brain_sessions`.`last_active` desc;

-- ---- brain_thoughts_detail ----
DROP VIEW IF EXISTS `brain_thoughts_detail`;
CREATE OR REPLACE ALGORITHM=UNDEFINED SQL SECURITY INVOKER VIEW `brain_thoughts_detail` AS select `t`.`turn_id` AS `turn_id`,`t`.`seq` AS `seq`,`t`.`ts` AS `thought_ts`,`tr`.`ts` AS `turn_ts`,`tr`.`session_id` AS `session_id`,`tr`.`platform` AS `platform`,`tr`.`author` AS `author`,`tr`.`machine` AS `machine`,`tr`.`message_count` AS `message_count`,`t`.`finish_reason` AS `finish_reason`,`t`.`reasoning_chars` AS `reasoning_chars`,`t`.`reasoning` AS `reasoning`,char_length(`tr`.`user_content`) AS `user_chars`,left(`tr`.`user_content`,200) AS `user_preview`,char_length(`tr`.`assistant_content`) AS `assistant_chars`,left(`tr`.`assistant_content`,200) AS `assistant_preview` from (`brain_thoughts` `t` left join `brain_turns` `tr` on(`tr`.`id` = `t`.`turn_id`));

-- ---- brain_thoughts_detail_bj ----
DROP VIEW IF EXISTS `brain_thoughts_detail_bj`;
CREATE OR REPLACE ALGORITHM=UNDEFINED SQL SECURITY INVOKER VIEW `brain_thoughts_detail_bj` AS select `t`.`turn_id` AS `turn_id`,`t`.`seq` AS `seq`,`t`.`ts` + interval 8 hour AS `thought_ts`,`tr`.`ts` + interval 8 hour AS `turn_ts`,`tr`.`session_id` AS `session_id`,`tr`.`platform` AS `platform`,`tr`.`author` AS `author`,`tr`.`machine` AS `machine`,`tr`.`message_count` AS `message_count`,`t`.`finish_reason` AS `finish_reason`,`t`.`reasoning_chars` AS `reasoning_chars`,`t`.`reasoning` AS `reasoning`,char_length(`tr`.`user_content`) AS `user_chars`,left(`tr`.`user_content`,200) AS `user_preview`,char_length(`tr`.`assistant_content`) AS `assistant_chars`,left(`tr`.`assistant_content`,200) AS `assistant_preview` from (`brain_thoughts` `t` left join `brain_turns` `tr` on(`tr`.`id` = `t`.`turn_id`));

-- ---- brain_turns_bj ----
DROP VIEW IF EXISTS `brain_turns_bj`;
CREATE OR REPLACE ALGORITHM=UNDEFINED SQL SECURITY INVOKER VIEW `brain_turns_bj` AS select `brain_turns`.`id` AS `id`,`brain_turns`.`session_id` AS `session_id`,`brain_turns`.`platform` AS `platform`,`brain_turns`.`author` AS `author`,`brain_turns`.`machine` AS `machine`,`brain_turns`.`truncated` AS `truncated`,`brain_turns`.`message_count` AS `message_count`,`brain_turns`.`message_bytes` AS `message_bytes`,`brain_turns`.`user_content` AS `user_content`,`brain_turns`.`assistant_content` AS `assistant_content`,`brain_turns`.`ts` + interval 8 hour AS `ts` from `brain_turns`;

-- ---- brain_turns_newest ----
DROP VIEW IF EXISTS `brain_turns_newest`;
CREATE OR REPLACE ALGORITHM=UNDEFINED SQL SECURITY INVOKER VIEW `brain_turns_newest` AS select `brain_turns`.`id` AS `id`,`brain_turns`.`session_id` AS `session_id`,`brain_turns`.`platform` AS `platform`,`brain_turns`.`author` AS `author`,`brain_turns`.`machine` AS `machine`,`brain_turns`.`user_content` AS `user_content`,`brain_turns`.`assistant_content` AS `assistant_content`,`brain_turns`.`ts` AS `ts` from `brain_turns` order by `brain_turns`.`ts` desc,`brain_turns`.`id` desc;

-- ---- brain_turn_messages_bj ----
DROP VIEW IF EXISTS `brain_turn_messages_bj`;
CREATE OR REPLACE ALGORITHM=UNDEFINED SQL SECURITY INVOKER VIEW `brain_turn_messages_bj` AS select `brain_turn_messages`.`id` AS `id`,`brain_turn_messages`.`turn_id` AS `turn_id`,`brain_turn_messages`.`seq` AS `seq`,`brain_turn_messages`.`role` AS `role`,`brain_turn_messages`.`tool_name` AS `tool_name`,`brain_turn_messages`.`tool_call_id` AS `tool_call_id`,`brain_turn_messages`.`raw_bytes` AS `raw_bytes`,`brain_turn_messages`.`body` AS `body`,`brain_turn_messages`.`ts` + interval 8 hour AS `ts` from `brain_turn_messages`;


-- ========================== 触发器 ==========================
-- platform / author 规范化 + 向量时间戳兜底；导入顺序在表之后。

DROP TRIGGER IF EXISTS `trg_brain_archive_platform_bi`;
DELIMITER $$
CREATE TRIGGER `trg_brain_archive_platform_bi` BEFORE INSERT ON `brain_archive` FOR EACH ROW SET NEW.platform = CASE LOWER(TRIM(COALESCE(NEW.platform, '')))
        WHEN '' THEN 'unknown'
        WHEN 'cli' THEN 'hermes-cli'
        WHEN 'tool' THEN 'hermes-cli'
        WHEN 'desktop' THEN 'hermes-desktop'
        WHEN 'local' THEN 'hermes-desktop'
        WHEN 'tui' THEN 'hermes-desktop'
        WHEN 'gui' THEN 'hermes-desktop'
        ELSE LEFT(LOWER(TRIM(NEW.platform)), 48)
    END$$
DELIMITER ;

DROP TRIGGER IF EXISTS `trg_brain_archive_platform_bu`;
DELIMITER $$
CREATE TRIGGER `trg_brain_archive_platform_bu` BEFORE UPDATE ON `brain_archive` FOR EACH ROW SET NEW.platform = CASE LOWER(TRIM(COALESCE(NEW.platform, '')))
        WHEN '' THEN 'unknown'
        WHEN 'cli' THEN 'hermes-cli'
        WHEN 'tool' THEN 'hermes-cli'
        WHEN 'desktop' THEN 'hermes-desktop'
        WHEN 'local' THEN 'hermes-desktop'
        WHEN 'tui' THEN 'hermes-desktop'
        WHEN 'gui' THEN 'hermes-desktop'
        ELSE LEFT(LOWER(TRIM(NEW.platform)), 48)
    END$$
DELIMITER ;

DROP TRIGGER IF EXISTS `trg_brain_sessions_platform_bi`;
DELIMITER $$
CREATE TRIGGER `trg_brain_sessions_platform_bi` BEFORE INSERT ON `brain_sessions` FOR EACH ROW SET NEW.platform = CASE LOWER(TRIM(COALESCE(NEW.platform, '')))
        WHEN '' THEN 'unknown'
        WHEN 'cli' THEN 'hermes-cli'
        WHEN 'tool' THEN 'hermes-cli'
        WHEN 'desktop' THEN 'hermes-desktop'
        WHEN 'local' THEN 'hermes-desktop'
        WHEN 'tui' THEN 'hermes-desktop'
        WHEN 'gui' THEN 'hermes-desktop'
        ELSE LEFT(LOWER(TRIM(NEW.platform)), 48)
    END$$
DELIMITER ;

DROP TRIGGER IF EXISTS `trg_brain_sessions_platform_bu`;
DELIMITER $$
CREATE TRIGGER `trg_brain_sessions_platform_bu` BEFORE UPDATE ON `brain_sessions` FOR EACH ROW SET NEW.platform = CASE LOWER(TRIM(COALESCE(NEW.platform, '')))
        WHEN '' THEN 'unknown'
        WHEN 'cli' THEN 'hermes-cli'
        WHEN 'tool' THEN 'hermes-cli'
        WHEN 'desktop' THEN 'hermes-desktop'
        WHEN 'local' THEN 'hermes-desktop'
        WHEN 'tui' THEN 'hermes-desktop'
        WHEN 'gui' THEN 'hermes-desktop'
        ELSE LEFT(LOWER(TRIM(NEW.platform)), 48)
    END$$
DELIMITER ;

DROP TRIGGER IF EXISTS `trg_brain_turns_author_bi`;
DELIMITER $$
CREATE TRIGGER `trg_brain_turns_author_bi` BEFORE INSERT ON `brain_turns` FOR EACH ROW SET NEW.author = CASE
        WHEN NEW.author LIKE '%@im.wechat'
            THEN CONCAT('wx:', LEFT(NEW.author, CHAR_LENGTH(NEW.author) - 10))
        WHEN NEW.author <> '' THEN NEW.author
        WHEN NEW.platform IN ('cron', 'oneshot', 'subagent')
            THEN CONCAT('sys:', NEW.platform)
        ELSE COALESCE(
            (SELECT author FROM brain_author_defaults WHERE machine = NEW.machine AND platform = NEW.platform),
            (SELECT author FROM brain_author_defaults WHERE machine = NEW.machine AND platform = '*'),
            (SELECT author FROM brain_author_defaults WHERE machine = '*' AND platform = NEW.platform),
            (SELECT author FROM brain_author_defaults WHERE machine = '*' AND platform = '*'),
            'unknown')
    END$$
DELIMITER ;

DROP TRIGGER IF EXISTS `trg_brain_turns_author_bu`;
DELIMITER $$
CREATE TRIGGER `trg_brain_turns_author_bu` BEFORE UPDATE ON `brain_turns` FOR EACH ROW SET NEW.author = CASE
        WHEN NEW.author LIKE '%@im.wechat'
            THEN CONCAT('wx:', LEFT(NEW.author, CHAR_LENGTH(NEW.author) - 10))
        WHEN NEW.author <> '' THEN NEW.author
        WHEN NEW.platform IN ('cron', 'oneshot', 'subagent')
            THEN CONCAT('sys:', NEW.platform)
        ELSE COALESCE(
            (SELECT author FROM brain_author_defaults WHERE machine = NEW.machine AND platform = NEW.platform),
            (SELECT author FROM brain_author_defaults WHERE machine = NEW.machine AND platform = '*'),
            (SELECT author FROM brain_author_defaults WHERE machine = '*' AND platform = NEW.platform),
            (SELECT author FROM brain_author_defaults WHERE machine = '*' AND platform = '*'),
            'unknown')
    END$$
DELIMITER ;

DROP TRIGGER IF EXISTS `trg_brain_turns_platform_bi`;
DELIMITER $$
CREATE TRIGGER `trg_brain_turns_platform_bi` BEFORE INSERT ON `brain_turns` FOR EACH ROW SET NEW.platform = CASE LOWER(TRIM(COALESCE(NEW.platform, '')))
        WHEN '' THEN 'unknown'
        WHEN 'cli' THEN 'hermes-cli'
        WHEN 'tool' THEN 'hermes-cli'
        WHEN 'desktop' THEN 'hermes-desktop'
        WHEN 'local' THEN 'hermes-desktop'
        WHEN 'tui' THEN 'hermes-desktop'
        WHEN 'gui' THEN 'hermes-desktop'
        ELSE LEFT(LOWER(TRIM(NEW.platform)), 48)
    END$$
DELIMITER ;

DROP TRIGGER IF EXISTS `trg_brain_turns_platform_bu`;
DELIMITER $$
CREATE TRIGGER `trg_brain_turns_platform_bu` BEFORE UPDATE ON `brain_turns` FOR EACH ROW SET NEW.platform = CASE LOWER(TRIM(COALESCE(NEW.platform, '')))
        WHEN '' THEN 'unknown'
        WHEN 'cli' THEN 'hermes-cli'
        WHEN 'tool' THEN 'hermes-cli'
        WHEN 'desktop' THEN 'hermes-desktop'
        WHEN 'local' THEN 'hermes-desktop'
        WHEN 'tui' THEN 'hermes-desktop'
        WHEN 'gui' THEN 'hermes-desktop'
        ELSE LEFT(LOWER(TRIM(NEW.platform)), 48)
    END$$
DELIMITER ;

DROP TRIGGER IF EXISTS `trg_brain_vector_index_embedded_bi`;
DELIMITER $$
CREATE TRIGGER `trg_brain_vector_index_embedded_bi` BEFORE INSERT ON `brain_vector_index` FOR EACH ROW SET NEW.embedded_at = CASE WHEN NEW.status = 'ok' THEN UTC_TIMESTAMP() ELSE NULL END$$
DELIMITER ;

DROP TRIGGER IF EXISTS `trg_brain_vector_index_embedded_bu`;
DELIMITER $$
CREATE TRIGGER `trg_brain_vector_index_embedded_bu` BEFORE UPDATE ON `brain_vector_index` FOR EACH ROW SET NEW.embedded_at = CASE WHEN NEW.status = 'ok' THEN UTC_TIMESTAMP() ELSE NULL END$$
DELIMITER ;
