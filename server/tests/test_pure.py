"""atbrains 插件纯函数自测（只依赖标准库 + 插件自身）。

不连接 MariaDB / Redis / Qdrant，不发网络请求；临时文件只用系统临时目录。
运行：`python3 selftest.py`（退出码 0 = 全部通过）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cli  # noqa: E402
from core import (  # noqa: E402
    AtbrainsStore,
    DEFAULT_CONFIG,
    _age_days,
    _benign_ddl_error,
    _clip,
    _cosine,
    _cursor_start,
    _window_start,
    _db_safe,
    _dotenv_get,
    _dotenv_value,
    _esc_like,
    _is_duplicate_error,
    _mask_secret,
    _memory_type,
    _utc_epoch,
    resolve_config,
    search_terms,
)

TEST_ENV = {
    "ATBRAINS_REDIS_PASSWORD": "",
    "ATBRAINS_MARIADB_PASSWORD": "",
    "HERMES_CUSTOM_API_COMMANDCODE_AI_API_KEY": "",
}


class SearchTermsTest(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(search_terms(""), [])
        self.assertEqual(search_terms(None), [])

    def test_latin_split_and_dedup(self) -> None:
        self.assertEqual(search_terms("redis redis"), ["redis"])
        self.assertEqual(search_terms("PostgreSQL postgresql"),
                         ["PostgreSQL", "postgresql"])   # 大小写敏感，各自成词
        self.assertEqual(search_terms("redis cache"), ["redis", "cache"])
        # 单字符不参与检索（LIKE 噪声太大）
        self.assertEqual(search_terms("a b c"), [])

    def test_cjk_bigrams_and_stopwords(self) -> None:
        # ≤4 字整段保留；更长的中文串按双字滑窗切分
        self.assertEqual(search_terms("向量检索"), ["向量检索"])
        self.assertEqual(search_terms("向量数据库"), ["向量", "量数", "数据", "据库"])
        self.assertEqual(search_terms("什么"), [])          # 整段是虚词
        self.assertEqual(search_terms("好的"), [])

    def test_max_terms(self) -> None:
        self.assertEqual(search_terms("aa bb cc dd", max_terms=2), ["aa", "bb"])

    def test_like_escape(self) -> None:
        self.assertEqual(_esc_like("50%_x"), "50\\%\\_x")
        self.assertEqual(_esc_like("a\\b"), "a\\\\b")


class TextHelpersTest(unittest.TestCase):
    def test_clip(self) -> None:
        self.assertEqual(_clip("  hello  ", 20), "hello")
        self.assertEqual(_clip("abcdef", 3), "abc…")
        self.assertEqual(_clip("ab", 10), "ab")

    def test_mask_secret(self) -> None:
        self.assertEqual(_mask_secret(""), "")
        self.assertEqual(_mask_secret("short"), "…")
        self.assertEqual(_mask_secret("12345678"), "…")
        self.assertEqual(_mask_secret("123456789"), "1234…")

    def test_memory_type(self) -> None:
        self.assertEqual(_memory_type("builtin-memory"), "profile")
        self.assertEqual(_memory_type("fact"), "knowledge")
        self.assertEqual(_memory_type("insight"), "knowledge")
        self.assertEqual(_memory_type("preference"), "knowledge")
        self.assertEqual(_memory_type("delegation"), "episodic")
        self.assertEqual(_memory_type("fact", "custom"), "custom")
        self.assertEqual(_memory_type("fact", "x" * 40), "x" * 16)


class TimeTest(unittest.TestCase):
    def test_utc_epoch_naive(self) -> None:
        dt = datetime(2024, 1, 1, 0, 0, 0)
        self.assertEqual(_utc_epoch(dt), 1704067200.0)
        self.assertEqual(_utc_epoch("2024-01-01T00:00:00"), 1704067200.0)
        self.assertEqual(_utc_epoch(1704067200), 1704067200.0)
        self.assertIsNone(_utc_epoch(None))
        self.assertIsNone(_utc_epoch("not-a-time"))

    @unittest.skipUnless(hasattr(time, "tzset"), "需要 time.tzset（Windows 无）")
    def test_utc_epoch_ignores_local_tz(self) -> None:
        old = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Asia/Shanghai"
            time.tzset()
            self.assertEqual(_utc_epoch(datetime(2024, 1, 1, 0, 0, 0)), 1704067200.0)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_age_days(self) -> None:
        now = 1704067200.0 + 86400.0
        self.assertEqual(_age_days(datetime(2024, 1, 1), now), 1.0)
        self.assertIsNone(_age_days(None, now))
        self.assertIsNone(_age_days("bad", now))
        # 未来时间不应产生负数年龄
        self.assertEqual(_age_days(datetime(2024, 1, 2), now), 0.0)


class CosineTest(unittest.TestCase):
    def test_cosine(self) -> None:
        self.assertAlmostEqual(_cosine([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertEqual(_cosine([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertEqual(_cosine([1.0], [1.0, 2.0]), 0.0)
        self.assertEqual(_cosine([0.0, 0.0], [1.0, 1.0]), 0.0)
        self.assertAlmostEqual(_cosine([1.0, 1.0], [1.0, 1.0]), 1.0)


class DotenvTest(unittest.TestCase):
    def test_values(self) -> None:
        cases = {
            " abc ": "abc",
            '"p@ss # 保留"': "p@ss # 保留",
            '"p@ss"  # 注释': "p@ss",
            "'p@ss'  # 注释": "p@ss",
            "p@ss#word": "p@ss#word",
            "p@ss # 注释": "p@ss",
            "p@ss\t# 注释": "p@ss",
            "# 全注释": "",
            "": "",
            '"a\\"b"': 'a"b',
            '"unterminated': '"unterminated',
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_dotenv_value(raw), want)

    def test_get_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(
                "# comment line\n"
                'export ATBRAINS_REDIS_PASSWORD="s3cr#t"  # prod\n'
                "ATBRAINS_MARIADB_PASSWORD=pw # note\n"
                "EMPTY=\n"
                "NOT_ME=x\n",
                encoding="utf-8",
            )
            self.assertEqual(_dotenv_get(env, "ATBRAINS_REDIS_PASSWORD"), "s3cr#t")
            self.assertEqual(_dotenv_get(env, "ATBRAINS_MARIADB_PASSWORD"), "pw")
            self.assertEqual(_dotenv_get(env, "EMPTY"), "")
            self.assertIsNone(_dotenv_get(env, "MISSING"))
        self.assertIsNone(_dotenv_get(Path("/nonexistent/.env"), "X"))


class ResolveConfigTest(unittest.TestCase):
    @mock.patch.dict(os.environ, TEST_ENV, clear=False)
    def test_merge_and_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "atbrains.json").write_text(json.dumps({
                "redis_port": "6380",
                "mariadb_db": "custom_db",
                "vector_enabled": False,
                "unknown_key": 1,
                "redis_host": None,
                "agent": "OMP",
            }), encoding="utf-8")
            (home / ".env").write_text(
                'ATBRAINS_REDIS_PASSWORD="r3d#is"  # 注释\n'
                "ATBRAINS_MARIADB_PASSWORD=maria # 注释\n"
                "HERMES_CUSTOM_API_COMMANDCODE_AI_API_KEY=\"dk\"\n",
                encoding="utf-8",
            )
            cfg = resolve_config(str(home))
            self.assertEqual(cfg["redis_port"], 6380)
            self.assertIsInstance(cfg["redis_port"], int)
            self.assertEqual(cfg["mariadb_db"], "custom_db")
            self.assertEqual(cfg["vector_enabled"], 0)
            self.assertNotIn("unknown_key", cfg)
            self.assertEqual(cfg["redis_host"], DEFAULT_CONFIG["redis_host"])  # None 不覆盖
            self.assertEqual(cfg["agent"], "OMP")  # 写入方身份可在 atbrains.json 设置
            self.assertEqual(DEFAULT_CONFIG["agent"], "")  # 空 = 回落 'hermes'
            self.assertEqual(cfg["redis_password"], "r3d#is")
            self.assertEqual(cfg["mariadb_password"], "maria")
            self.assertEqual(cfg["distill_api_key"], "dk")
            self.assertEqual(cfg["hermes_home"], str(home))
            self.assertEqual(cfg["turn_messages_enabled"], 1)

    @mock.patch.dict(os.environ, TEST_ENV, clear=False)
    def test_invalid_json_and_bad_int(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "atbrains.json").write_text('{"redis_port": "nope"}', encoding="utf-8")
            cfg = resolve_config(str(home))
            self.assertEqual(cfg["redis_port"], DEFAULT_CONFIG["redis_port"])
            (home / "atbrains.json").write_text("{not json", encoding="utf-8")
            cfg = resolve_config(str(home))
            self.assertEqual(cfg["mariadb_db"], DEFAULT_CONFIG["mariadb_db"])


class CursorTest(unittest.TestCase):
    MSGS = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]

    def test_normal(self) -> None:
        self.assertEqual(_cursor_start(self.MSGS, 0), 0)
        self.assertEqual(_cursor_start(self.MSGS, 2), 2)
        self.assertEqual(_cursor_start(self.MSGS, 4), 4)

    def test_compressed_rewind(self) -> None:
        # 历史变短（压缩/回退）：退回最后一个 user
        self.assertEqual(_cursor_start(self.MSGS[:3], 10), 2)
        self.assertEqual(_cursor_start([{"role": "assistant"}], 10), 0)

    def test_bad_values(self) -> None:
        self.assertEqual(_cursor_start(self.MSGS, -5), 0)
        self.assertEqual(_cursor_start(self.MSGS, "bad"), 0)


class WindowStartTest(unittest.TestCase):
    """尾部窗口 + 绝对游标（dsh 插件只传最后 N 条）。"""

    MSGS = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]

    def test_full_list(self) -> None:
        # 窗口即全量（老客户端）：行为与 _cursor_start 一致
        self.assertEqual(_window_start(self.MSGS, 0, None), (0, 0))
        self.assertEqual(_window_start(self.MSGS, 2, None), (2, 2))
        self.assertEqual(_window_start(self.MSGS, 4, 4), (4, 4))

    def test_tail_window(self) -> None:
        # 10 条会话只传最后 4 条（绝对下标 6..9）
        self.assertEqual(_window_start(self.MSGS, 6, 10), (0, 6))
        self.assertEqual(_window_start(self.MSGS, 8, 10), (2, 8))
        # 游标已到窗口末端：没有增量，但游标不能漂
        self.assertEqual(_window_start(self.MSGS, 10, 10), (4, 10))

    def test_cursor_behind_window(self) -> None:
        # 会话 1000 条、只传最后 4 条（窗口起点 996），游标 400 落在窗口之前：
        # 中间缺口补不回来，从窗口起点续写，游标跟到 996
        self.assertEqual(_window_start(self.MSGS, 400, 1000), (0, 996))

    def test_compressed_rewind(self) -> None:
        # 历史被压缩：total 变小、游标比总数还大 → 退回最后一个 user
        self.assertEqual(_window_start(self.MSGS[:3], 10, 3), (2, 2))
        self.assertEqual(_window_start([{"role": "assistant"}], 10, 1), (0, 0))

    def test_bad_values(self) -> None:
        self.assertEqual(_window_start(self.MSGS, "bad", 4), (0, 0))
        self.assertEqual(_window_start(self.MSGS, -5, 10), (0, 6))
        # total 小于消息数按 total=len 处理，不产生负偏移
        self.assertEqual(_window_start(self.MSGS, 2, 1), (2, 2))


class DbSafeTest(unittest.TestCase):
    def test_surrogate(self) -> None:
        bad = 'ok\ud800bad'
        safe = _db_safe(bad)
        self.assertNotIn("\ud800", safe)
        safe.encode("utf-8")  # 不再抛 UnicodeEncodeError
        self.assertEqual(_db_safe("plain"), "plain")
        self.assertEqual(_db_safe(5), 5)
        self.assertEqual(_db_safe(None), None)
        self.assertEqual(_db_safe(b"bytes"), b"bytes")


class ErrorPredicateTest(unittest.TestCase):
    def test_predicates(self) -> None:
        self.assertTrue(_is_duplicate_error(Exception(1062, "dup")))
        self.assertFalse(_is_duplicate_error(Exception(1064, "syntax")))
        self.assertFalse(_is_duplicate_error(Exception()))
        self.assertTrue(_benign_ddl_error(Exception(1050, "exists")))
        self.assertFalse(_benign_ddl_error(Exception(1064, "syntax")))


class StorePureTest(unittest.TestCase):
    def test_redis_key_prefix(self) -> None:
        store = AtbrainsStore(dict(DEFAULT_CONFIG))
        self.assertEqual(store._rk("hot", "index"), "brain:hot:index")
        cfg = dict(DEFAULT_CONFIG)
        cfg["redis_key_prefix"] = "brain:server"
        self.assertEqual(AtbrainsStore(cfg)._rk("hot", "index"), "brain:server:hot:index")
        cfg["redis_key_prefix"] = "server:"
        self.assertEqual(AtbrainsStore(cfg)._rk("session", "s1"), "server:session:s1")

    def test_compose(self) -> None:
        cfg = dict(DEFAULT_CONFIG)
        store = AtbrainsStore(cfg)
        mems = [{"kind": "fact", "importance": 7, "content": "数据库是 MariaDB",
                 "tags": "db", "machine": "host-a"}]
        turns = [{"ts": datetime(2024, 1, 2, 3, 4, 5), "machine": "host-a",
                  "user_content": "上次说的库是什么？"}]
        text = store._compose(mems, turns)
        self.assertIn("MariaDB", text)
        self.assertIn("2024-01-02", text)
        self.assertIn("[host-a]", text)
        self.assertIn("上次说的库是什么", text)
        self.assertEqual(store._compose([], []), "")

    def test_compose_budget(self) -> None:
        cfg = dict(DEFAULT_CONFIG)
        cfg["prefetch_max_chars"] = 60
        store = AtbrainsStore(cfg)
        mems = [{"kind": "fact", "importance": 5, "content": "x" * 500,
                 "tags": "", "machine": ""}]
        text = store._compose(mems, [])
        self.assertTrue(text.endswith("…"))
        self.assertLessEqual(len(text), 62)


class GraphSlotPureTest(unittest.TestCase):
    """图谱实体过滤 + 蒸馏槽位补抽的解析/校验（不连库、不发请求）。"""

    def test_entity_filter(self) -> None:
        ok = AtbrainsStore._graph_entity_ok
        self.assertTrue(ok("MariaDB"))
        self.assertTrue(ok("atbrains 记忆库"))
        self.assertFalse(ok("用户"))
        self.assertFalse(ok("User"))
        self.assertFalse(ok("x"))
        self.assertFalse(ok(".docx"))
        self.assertFalse(ok("a" * 61))

    def test_slot_pass_validation(self) -> None:
        store = AtbrainsStore(dict(DEFAULT_CONFIG))
        payload = json.dumps({"slots": [
            {"index": 0, "subject": "服务器", "predicate": "SSH 端口", "value": "7622"},
            {"index": 1, "subject": "记忆库", "predicate": "schema 版本", "value": "v10。" + "很长" * 80},
            {"index": 2, "subject": "", "predicate": "y", "value": "z"},
            {"index": 9, "subject": "越界", "predicate": "y", "value": "z"},
        ]}, ensure_ascii=False)
        with mock.patch.object(AtbrainsStore, "_distill_llm_chat", return_value=payload):
            got = store._distill_slot_pass(["a", "b", "c"])
        self.assertEqual(got[0], {"subject": "服务器", "predicate": "SSH 端口", "value": "7622"})
        self.assertNotIn(1, got)   # 超长值 = 多句叙述，形状校验必须丢掉
        self.assertNotIn(2, got)   # 空 subject
        self.assertNotIn(9, got)   # 越界 index

    def test_slot_pass_failure_is_empty(self) -> None:
        store = AtbrainsStore(dict(DEFAULT_CONFIG))
        with mock.patch.object(AtbrainsStore, "_distill_llm_chat", side_effect=RuntimeError("down")):
            self.assertEqual(store._distill_slot_pass(["a"]), {})


class ClaimLayerTest(unittest.TestCase):
    def test_slot_key_stable_and_distinct(self) -> None:
        a = AtbrainsStore.claim_slot_key("server", "服务器", "时区", "fact")
        b = AtbrainsStore.claim_slot_key("server", "服务器", "时区", "fact")
        c = AtbrainsStore.claim_slot_key("server", "服务器", "时区", "preference")
        d = AtbrainsStore.claim_slot_key("r730", "服务器", "时区", "fact")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)
        self.assertEqual(len(a), 32)

    def test_json_list_shapes(self) -> None:
        f = AtbrainsStore._json_list
        self.assertEqual(json.loads(f(None)), [])
        self.assertEqual(json.loads(f("")), [])
        self.assertEqual(json.loads(f(["a", "b"])), ["a", "b"])
        self.assertEqual(json.loads(f('["x","y"]')), ["x", "y"])
        self.assertEqual(json.loads(f("单条")), ["单条"])
        self.assertEqual(json.loads(f(["a", " "])), ["a"])


class CliTest(unittest.TestCase):
    def test_fmt_bytes(self) -> None:
        self.assertEqual(cli._fmt_bytes(0), "0 B")
        self.assertEqual(cli._fmt_bytes(512), "512 B")
        self.assertEqual(cli._fmt_bytes(1024), "1.0 KB")
        self.assertEqual(cli._fmt_bytes(1536), "1.5 KB")
        self.assertEqual(cli._fmt_bytes(1024 * 1024), "1.0 MB")
        self.assertEqual(cli._fmt_bytes(1024 ** 3), "1.0 GB")


if __name__ == "__main__":
    unittest.main(verbosity=2)
