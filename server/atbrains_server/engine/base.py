"""连接池（Redis / MariaDB）、执行器、缓存失效、schema 就绪检查。"""
from __future__ import annotations

from contextlib import contextmanager
import logging
import queue
import socket
import threading
import time
from typing import Any, Iterator, Optional

from ..config import resolve_config  # noqa: F401
from ..context import RequestContext, normalize_agent, normalize_machine
from ..schema import (  # noqa: F401
    SCHEMA_VERSION,
    _DDL,
    _MIGRATIONS,
    _SCHEMA_READY,
    _benign_ddl_error,
)
from ..util import _cfg_flag, _db_safe  # noqa: F401

logger = logging.getLogger("atbrains")


class BaseMixin:
    """连接池（Redis / MariaDB）、执行器、缓存失效、schema 就绪检查。"""

    def __init__(self, config: Optional[dict[str, Any]] = None, *,
                 start_workers: Optional[bool] = None,
                 auto_migrate: Optional[bool] = None,
                 vec_wake: Optional[threading.Event] = None):
        self.cfg: dict[str, Any] = dict(config or resolve_config())
        self.machine: str = str(self.cfg.get("machine") or "").strip() or socket.gethostname()
        self.agent: str = (str(self.cfg.get("agent") or "default").strip().lower() or "default")[:32]
        self._auto_migrate: bool = _cfg_flag(self.cfg, "auto_migrate", 1) if auto_migrate is None else bool(auto_migrate)
        self._workers_wanted: bool = (
            _cfg_flag(self.cfg, "background_workers", 0) if start_workers is None else bool(start_workers)
        )
        self._redis = None
        self._redis_lock = threading.Lock()
        pool_size = max(1, int(self.cfg.get("db_pool_size") or 8))
        self._db_pool: queue.LifoQueue[tuple[Any, float]] = queue.LifoQueue(maxsize=pool_size)
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self._vec_wake = vec_wake if vec_wake is not None else threading.Event()
        self._drainer_started = False
        self._drainer_lock = threading.Lock()
        self._maint_started = False
        self._maint_lock = threading.Lock()
        self._distill_started = False
        self._distill_lock = threading.Lock()
        self._vsec: Optional[dict] = None
        self._vsec_ts = 0.0
        self._stats_cache: Optional[tuple[float, dict[str, Any]]] = None
        self._vector_cache: Optional[tuple[float, dict[str, Any]]] = None
        self._author_defaults_cache: Optional[tuple[float, list[dict[str, Any]]]] = None
        self._cache_ttl = 60.0
        self._qcache_ver = 0
        self._vec_dirty = True
        self._drain_ticks = 0
        self._drain_last_remaining = 0
        self._drain_remaining_ts = 0.0
        self._qdrant_ready = False

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

    _PING_INTERVAL = 30.0
    _RETRY_CODES = (2006, 2013, 2014)

    def _new_conn(self) -> Any:
        import pymysql

        return pymysql.connect(
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

    def _checkout_conn(self) -> Any:
        now = time.monotonic()
        while True:
            try:
                conn, last_used = self._db_pool.get_nowait()
            except queue.Empty:
                return self._new_conn()
            if now - last_used < self._PING_INTERVAL:
                return conn
            try:
                conn.ping()
                return conn
            except Exception:  # noqa: BLE001
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def _return_conn(self, conn: Any) -> None:
        if conn is None:
            return
        try:
            self._db_pool.put_nowait((conn, time.monotonic()))
        except queue.Full:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    @contextmanager
    def _borrow_conn(self) -> Iterator[Any]:
        conn = self._checkout_conn()
        healthy = True
        try:
            yield conn
        except BaseException:
            healthy = False
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            if healthy:
                self._return_conn(conn)

    def _exec(self, sql: str, params: tuple = (), *, fetch: bool = False):
        import pymysql

        safe = tuple(_db_safe(p) for p in params) if params else params
        last_exc: Optional[BaseException] = None
        for attempt in (0, 1):
            conn = self._checkout_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, safe)
                    res = cur.fetchall() if fetch else (cur.rowcount, cur.lastrowid)
                self._return_conn(conn)
                return res
            except pymysql.err.OperationalError as exc:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                code = exc.args[0] if exc.args else 0
                if attempt == 0 and code in self._RETRY_CODES:
                    last_exc = exc
                    continue
                raise
            except BaseException:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                raise
        assert last_exc is not None
        raise last_exc

    def _identity(self, ctx: Optional[RequestContext] = None) -> tuple[str, str, str]:
        """解析本次调用的有效 (machine, agent, role)；直连调用未传 ctx 时按本机 admin 处理。"""
        if ctx is None:
            return self.machine[:48], self.agent, "admin"
        mach = normalize_machine(ctx.machine, self.machine)
        ag = normalize_agent(ctx.agent, self.agent)
        role = "admin" if ctx.role == "admin" else "agent"
        return mach, ag, role

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """从连接池借出一条连接执行显式事务（成功提交归还，异常回滚）。"""
        conn = self._checkout_conn()
        try:
            conn.begin()
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = None
            raise
        finally:
            if conn is not None:
                self._return_conn(conn)

    def _invalidate_caches(self, *, vec_dirty: bool = False, bump_qver: bool = True) -> None:
        """写路径后让 stats/vector_status 与查询缓存立即失效。"""
        self._stats_cache = None
        self._vector_cache = None
        self._qcache_ver += 1
        if vec_dirty:
            self._vec_dirty = True
        if bump_qver and self._redis is not None and hasattr(self, "_rk"):
            try:
                self._redis.incr(self._rk("qver"))
            except Exception:  # noqa: BLE001
                pass

    def _wake_drainer(self) -> None:
        try:
            self._vec_wake.set()
        except Exception:  # noqa: BLE001
            pass

    def migrate(self) -> int:
        """显式执行建表与幂等迁移（返回迁移后的 schema_version）。"""
        return self.ensure_schema(force_migrate=True)

    def ensure_schema(self, *, force_migrate: bool = False) -> int:
        if self._schema_ready and not force_migrate:
            if self._workers_wanted:
                self.start_workers()
            return SCHEMA_VERSION
        ready_key = (str(self.cfg.get("mariadb_host") or ""), int(self.cfg.get("mariadb_port") or 0),
                     str(self.cfg.get("mariadb_db") or ""), SCHEMA_VERSION)
        with self._schema_lock:
            if not force_migrate and (self._schema_ready or ready_key in _SCHEMA_READY):
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
            with self._borrow_conn() as conn:
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
        while True:
            try:
                conn, _ = self._db_pool.get_nowait()
            except queue.Empty:
                break
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        with self._redis_lock:
            if self._redis is not None:
                try:
                    self._redis.close()
                except Exception:  # noqa: BLE001
                    pass
                self._redis = None
