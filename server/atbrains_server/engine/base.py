"""连接（Redis / MariaDB）、执行器、缓存失效、schema 就绪检查。"""
from __future__ import annotations

import logging
from typing import Any
from typing import Optional
import socket
import threading
import time

from ..config import (  # noqa: F401
    resolve_config,
)
from ..schema import (  # noqa: F401
    SCHEMA_VERSION,
    _DDL,
    _MIGRATIONS,
    _SCHEMA_READY,
    _benign_ddl_error,
)
from ..util import (  # noqa: F401
    _cfg_flag,
    _db_safe,
)

logger = logging.getLogger("atbrains")

class BaseMixin:
    """连接（Redis / MariaDB）、执行器、缓存失效、schema 就绪检查。"""

    def __init__(self, config: Optional[dict[str, Any]] = None, *,
                 start_workers: Optional[bool] = None,
                 auto_migrate: Optional[bool] = None,
                 vec_wake: Optional[threading.Event] = None):
        self.cfg: dict[str, Any] = dict(config or resolve_config())
        self.machine: str = str(self.cfg.get("machine") or "").strip() or socket.gethostname()
        # agent = 写入方身份，与 machine（主机）解耦；服务端由令牌或显式参数指定，默认 'default'。
        self.agent: str = (str(self.cfg.get("agent") or "default").strip().lower() or "default")[:32]
        self._auto_migrate: bool = _cfg_flag(self.cfg, "auto_migrate", 1) if auto_migrate is None else bool(auto_migrate)
        self._workers_wanted: bool = (
            _cfg_flag(self.cfg, "background_workers", 0) if start_workers is None else bool(start_workers)
        )
        self._redis = None
        self._redis_lock = threading.Lock()
        self._tls = threading.local()
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self._vec_wake = vec_wake if vec_wake is not None else threading.Event()
        self._drainer_started = False
        self._drainer_lock = threading.Lock()
        self._maint_started = False           # 维护线程每进程最多一个
        self._maint_lock = threading.Lock()
        self._distill_started = False         # 蒸馏线程每进程最多一个
        self._distill_lock = threading.Lock()
        self._vsec: Optional[dict] = None    # 向量层 key 缓存（来源：qdrant 容器配置）
        self._vsec_ts = 0.0
        # 控制台/状态轮询的 TTL 缓存：stats()/vector_status() 是全表聚合，5 秒一次轮询
        # 不值得重算；写路径（remember/forget/prune/drain）会主动失效。
        self._stats_cache: Optional[tuple[float, dict[str, Any]]] = None
        self._vector_cache: Optional[tuple[float, dict[str, Any]]] = None
        self._cache_ttl = 10.0
        self._vec_dirty = True                # 有未向量化写入；没有就不必扫全表找 pending
        self._drain_ticks = 0                 # 空闲 drain 计数（每 10 次仍全量复查一轮）
        self._drain_last_remaining = 0
        self._drain_remaining_ts = 0.0
        self._qdrant_ready = False            # qdrant_ensure_collection 每个 store 只做一次

    def redis(self):
        import redis as redis_lib

        with self._redis_lock:
            if self._redis is None:
                self._redis = redis_lib.Redis(
                    host=self.cfg["redis_host"],
                    port=int(self.cfg["redis_port"]),
                    db=int(self.cfg.get("redis_db", 0)),
                    password=self.cfg.get("redis_password") or None,
                    socket_connect_timeout=3,
                    socket_timeout=3,
                    decode_responses=True,
                    health_check_interval=30,
                )
            return self._redis

    # 存活探测节流：每条语句都 ping 会让所有路径的 DB 往返翻倍；30 秒内复用连接
    # 不再探测，空闲超过该窗口才 ping（reconnect=True）。连接真断时由 _exec 重试兜底。
    _PING_INTERVAL = 30.0

    _RETRY_CODES = (2006, 2013, 2014)   # server gone away / lost / 连接被关

    def _conn(self):
        import pymysql

        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            now = time.monotonic()
            if now - float(getattr(self._tls, "pinged_at", 0.0) or 0.0) < self._PING_INTERVAL:
                return conn
            try:
                conn.ping()
                self._tls.pinged_at = now
                return conn
            except Exception:  # noqa: BLE001
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._tls.conn = None
        conn = pymysql.connect(
            host=self.cfg["mariadb_host"],
            port=int(self.cfg["mariadb_port"]),
            user=self.cfg["mariadb_user"],
            password=self.cfg.get("mariadb_password") or "",
            database=self.cfg["mariadb_db"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            connect_timeout=3,
            read_timeout=10,
            write_timeout=10,
        )
        self._tls.conn = conn
        self._tls.pinged_at = time.monotonic()
        return conn

    def _exec(self, sql: str, params: tuple = (), *, fetch: bool = False):
        import pymysql

        safe = tuple(_db_safe(p) for p in params) if params else params
        last_exc: Optional[BaseException] = None
        for attempt in (0, 1):
            conn = self._conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, safe)
                    if fetch:
                        return cur.fetchall()
                    return cur.rowcount, cur.lastrowid
            except pymysql.err.OperationalError as exc:
                code = exc.args[0] if exc.args else 0
                if attempt == 0 and code in self._RETRY_CODES:
                    last_exc = exc
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._tls.conn = None
                    self._tls.pinged_at = 0.0
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def _invalidate_caches(self, *, vec_dirty: bool = False) -> None:
        """写路径后让 stats/vector_status 的 TTL 缓存立即失效。

        vec_dirty=True 同时告诉 drainer「有新的待向量化写入」，它就不必在每个
        60 秒定时唤醒里全表扫 pending。
        """
        self._stats_cache = None
        self._vector_cache = None
        if vec_dirty:
            self._vec_dirty = True

    def _wake_drainer(self) -> None:
        try:
            self._vec_wake.set()
        except Exception:  # noqa: BLE001
            pass

    def migrate(self) -> int:
        """显式执行建表与幂等迁移（返回迁移后的 schema_version）。"""
        return self.ensure_schema(force_migrate=True)

    def ensure_schema(self, *, force_migrate: bool = False) -> int:
        ready_key = (str(self.cfg.get("mariadb_host") or ""), int(self.cfg.get("mariadb_port") or 0),
                     str(self.cfg.get("mariadb_db") or ""), SCHEMA_VERSION)
        with self._schema_lock:
            if self._schema_ready or ready_key in _SCHEMA_READY:
                self._schema_ready = True
                if self._workers_wanted:
                    self.start_workers()
                return SCHEMA_VERSION
            if not force_migrate and not self._auto_migrate:
                self._schema_ready = True
                _SCHEMA_READY.add(ready_key)
                if self._workers_wanted:
                    self.start_workers()
                return SCHEMA_VERSION
            conn = self._conn()
            with conn.cursor() as cur:
                for ddl in _DDL:
                    cur.execute(ddl)
                cur.execute("SELECT meta_value FROM brain_meta WHERE meta_key = 'schema_version'")
                row = cur.fetchone()
                current = int(row["meta_value"]) if row and str(row["meta_value"]).isdigit() else 0
                if current < SCHEMA_VERSION:
                    cur.execute("SELECT GET_LOCK('brain_schema_migrate', 15) AS got")
                    got = (cur.fetchone() or {}).get("got")
                    try:
                        # 拿到锁后重读：可能已被其它进程迁移完
                        cur.execute("SELECT meta_value FROM brain_meta WHERE meta_key = 'schema_version'")
                        row = cur.fetchone()
                        current = int(row["meta_value"]) if row and str(row["meta_value"]).isdigit() else 0
                        for ver in sorted(_MIGRATIONS):
                            if ver <= current:
                                continue
                            desc, stmts = _MIGRATIONS[ver]
                            for stmt in stmts:
                                try:
                                    cur.execute(stmt)
                                except Exception as exc:  # noqa: BLE001
                                    if not _benign_ddl_error(exc):
                                        raise
                                    logger.debug("atbrains: 迁移 v%s 语句已存在，跳过: %s", ver, exc)
                            logger.info("atbrains: schema 迁移 v%s 完成（%s）", ver, desc)
                        cur.execute(
                            "INSERT INTO brain_meta (meta_key, meta_value) VALUES ('schema_version', %s) "
                            "ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)",
                            (str(SCHEMA_VERSION),))
                    finally:
                        if got:
                            cur.execute("SELECT RELEASE_LOCK('brain_schema_migrate')")
            self._schema_ready = True
            _SCHEMA_READY.add(ready_key)
        if self._workers_wanted:
            self.start_workers()
        return SCHEMA_VERSION

    def start_workers(self) -> None:
        """显式启动进程内后台协调线程（向量 drainer + 维护循环 + 可选 LLM 蒸馏）。"""
        self.start_vector_drainer()
        self.start_maintenance()
        self.start_distill()

    def schema_version(self) -> int:
        self.ensure_schema()
        rows = self._exec(
            "SELECT meta_value FROM brain_meta WHERE meta_key = 'schema_version'", fetch=True)
        try:
            return int(rows[0]["meta_value"]) if rows else 0
        except (KeyError, TypeError, ValueError):
            return 0

    def close(self) -> None:
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._tls.conn = None
        with self._redis_lock:
            if self._redis is not None:
                try:
                    self._redis.close()
                except Exception:  # noqa: BLE001
                    pass
                self._redis = None
