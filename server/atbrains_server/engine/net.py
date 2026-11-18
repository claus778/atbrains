"""出站 HTTP：keep-alive 连接复用、私网直连、向量服务凭据。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from typing import Optional
import json
import re
import threading
import time

from ..config import default_qdrant_config_candidates
from ..util import (  # noqa: F401
    BrainHTTPError,
    _cfg_flag,
)

logger = logging.getLogger("atbrains")

# 本地/私网地址不走 HTTP(S)_PROXY：Qdrant、内网 Memory Service 都是直连，
# 而 Hermes 常带着代理环境变量启动（本机实测：代理把 127.0.0.1:6333 变成 502、每次 3 秒）。
_NO_PROXY_OPENER = None

def _is_private_host(host: str) -> bool:
    host = str(host or "").strip().strip("[]").lower()
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
            return True
    return False

#
# 公网目标（embed / rerank / 蒸馏 / 远程 Memory Service）每次请求都新建 TCP+TLS：
# 本机实测握手约占总耗时的一半。这里按线程缓存连接复用（线程本地而非共享池：网关是
# 多线程的，socket 不能并发共享，共享池要么加锁串行化、要么一条坏连接带塌整池；
# 线程本地零争用、坏连接只影响自己那一次，代价是每线程每主机最多 1 条空闲连接）。
# 只对「公网目标 + 进程环境没有给该主机配代理」生效，其余路径一个字节都不变。
_HTTP_TLS = threading.local()

_HTTP_KEEPALIVE_MAX_HOSTS = 4
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024

class _KeepAliveBypass(Exception):
    """keep-alive 快路径处理不了的响应（3xx）：关连接，回落原 opener 逻辑。"""

def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass

def _proxy_configured_for(host: str) -> bool:
    """进程环境是否会给该主机走代理（有代理变量且未被 no_proxy 排除）。

    走代理时必须回落 urllib：http.client 是直连，会绕过代理，语义就变了。
    """
    import urllib.request
    try:
        if not urllib.request.getproxies():
            return False
        return not urllib.request.proxy_bypass(host)
    except Exception:  # noqa: BLE001
        return True   # 判定不出来就当有代理：退回原路径，绝不自作主张直连


class NetMixin:
    """出站 HTTP：keep-alive 连接复用、私网直连、向量服务凭据。"""

    def _vector_secrets(self) -> dict[str, str]:
        """向量层凭据：优先 cfg/环境变量（qdrant_api_key / embed_api_key），回落 Qdrant yaml。"""
        now = time.time()
        if self._vsec is not None and now - self._vsec_ts < 300:
            return self._vsec
        qkey = str(self.cfg.get("qdrant_api_key") or "").strip()
        ekey = str(self.cfg.get("embed_api_key") or "").strip()
        if not (qkey and ekey):
            raw_path = str(self.cfg.get("qdrant_config_file") or "").strip()
            candidates = [Path(raw_path).expanduser()] if raw_path else list(default_qdrant_config_candidates())
            yaml_key = ""
            for p in candidates:
                if not p.is_file():
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    section = ""
                    for raw in text.splitlines():
                        line = raw.split("#", 1)[0].rstrip()
                        if not line.strip():
                            continue
                        if len(line) - len(line.lstrip()) == 0:
                            section = line.strip().rstrip(":")
                        elif section == "service":
                            m = re.match(r"\s*api_key\s*:\s*(.+?)\s*$", line)
                            if m:
                                val = m.group(1).strip().strip('"').strip("'")
                                if val and val.lower() not in ("null", "~"):
                                    yaml_key = val
                                    break
                except Exception as exc:  # noqa: BLE001
                    logger.debug("atbrains: 读取 qdrant 配置文件 %s 失败: %s", p, exc)
                if yaml_key:
                    break
            qkey = qkey or yaml_key
            ekey = ekey or yaml_key
        out: dict[str, str] = {}
        if qkey:
            out["qdrant"] = qkey
        if ekey:
            out["embed"] = ekey
        self._vsec, self._vsec_ts = out, now
        return out

    def _http_keepalive(self, method: str, url: str, parts, hdrs: dict,
                        data: Optional[bytes], timeout: float) -> tuple[int, str]:
        """公网直连的 keep-alive 快路径：复用 TCP+TLS，连接失效时重连后再试一次（只一次）。

        返回 (status, body)。快路径不该处理的响应（3xx 重定向）抛 _KeepAliveBypass，
        由 _http_json 回落原来的 urllib 路径；非 2xx 与原实现一样抛 BrainHTTPError
        （401 同时失效 key 缓存）；网络层异常包成 URLError（与 urlopen 同一异常面）。
        """
        import http.client
        import ssl as ssl_mod
        import urllib.error

        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise _KeepAliveBypass(f"scheme {scheme!r}")
        host = parts.hostname or ""
        key = (scheme, host, parts.port)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = 15.0
        try:
            idle_s = float(self.cfg.get("http_keepalive_idle_s") or 60)
        except (TypeError, ValueError):
            idle_s = 60.0
        # 「对端已关/半关」类错误：换一条新连接重试一次即可自愈
        retry_errors = (http.client.RemoteDisconnected, http.client.BadStatusLine,
                        http.client.CannotSendRequest, http.client.IncompleteRead,
                        BrokenPipeError, ConnectionResetError, ssl_mod.SSLError)
        pool = getattr(_HTTP_TLS, "pool", None)
        if pool is None:
            pool = {}
            _HTTP_TLS.pool = pool
        conn = None
        from_pool = False
        last_exc: Optional[BaseException] = None
        for attempt in (0, 1):
            if conn is None:
                entry = pool.pop(key, None)
                if entry is not None:
                    cand, last_used, cand_timeout = entry
                    if (float(cand_timeout) == timeout and cand.sock is not None
                            and time.monotonic() - float(last_used) <= idle_s):
                        conn, from_pool = cand, True
                    else:
                        _close_quietly(cand)
            if conn is None:
                cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
                conn = cls(host, parts.port, timeout=timeout)
                from_pool = False
            elif from_pool:
                # 复用连接：读超时按本次调用重设，绝不让上一次的超时留在 socket 上
                conn.timeout = timeout
                if conn.sock is not None:
                    try:
                        conn.sock.settimeout(timeout)
                    except OSError:
                        pass
            try:
                conn.request(method, path, body=data, headers=hdrs)
                resp = conn.getresponse()
                status = int(resp.status)
                raw = resp.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise ValueError(f"upstream response exceeds {_MAX_RESPONSE_BYTES} bytes")
                body = raw.decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001 —— 一律先关连接：复用过的连接不可信
                _close_quietly(conn)
                pool.pop(key, None)
                conn, last_exc = None, exc
                # 复用来的连接上任何 OSError（含本端 socket 已关的 EBADF、半关连接上的
                # 读超时）都当作「这条连接已废」，换新连接重试一次；本次新建连接上的
                # 普通 OSError 不重试（避免把已经发出去的请求再打一遍）。
                retry = isinstance(exc, retry_errors) or (from_pool and isinstance(exc, OSError))
                from_pool = False
                if attempt == 0 and retry:
                    continue
                break
            if 200 <= status < 300:
                if resp.will_close:
                    _close_quietly(conn)
                else:
                    if key not in pool and len(pool) >= _HTTP_KEEPALIVE_MAX_HOSTS:
                        _close_quietly(pool.pop(next(iter(pool)))[0])
                    pool[key] = (conn, time.monotonic(), timeout)
                return status, body
            _close_quietly(conn)
            if status in (301, 302, 303, 307, 308):
                raise _KeepAliveBypass(f"HTTP {status} 交给 urllib 跟随重定向")
            if status == 401:      # key 可能刚被轮换：让缓存失效，下次重读
                self._vsec, self._vsec_ts = None, 0.0
            raise BrainHTTPError(status, url, body[:300])
        raise urllib.error.URLError(last_exc if last_exc is not None else "keep-alive 请求失败")

    def _http_json(self, method: str, url: str, *, headers: Optional[dict] = None,
                   payload: Optional[dict] = None, timeout: float = 15,
                   no_proxy: Optional[bool] = None) -> tuple[int, dict]:
        import urllib.error
        import urllib.parse
        import urllib.request
        global _NO_PROXY_OPENER
        host = ""
        if no_proxy is None:
            # 本地/私网目标自动绕过代理；公网 embed/rerank 仍按环境变量走
            host = urllib.parse.urlsplit(url).hostname or ""
            no_proxy = _is_private_host(host)
        # keep-alive 只用于「公网 + 进程环境没给该主机配代理」；开关关掉时这里不产生任何副作用
        parts = None
        keepalive = _cfg_flag(self.cfg, "http_keepalive_enabled", 1) and not no_proxy
        if keepalive:
            parts = urllib.parse.urlsplit(url)
            host = parts.hostname or host
            keepalive = not _proxy_configured_for(host)
        opener = None
        if no_proxy:
            if _NO_PROXY_OPENER is None:
                _NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            opener = _NO_PROXY_OPENER
        ua = str(self.cfg.get("http_user_agent") or "atbrains-server/3.0")
        hdrs = {"Accept": "application/json", "User-Agent": ua}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        if keepalive and parts is not None:
            try:
                status, body = self._http_keepalive(method, url, parts, hdrs, data, timeout)
            except _KeepAliveBypass:
                pass    # 例：3xx 需要跟随重定向 —— 交给下面的 urllib 路径，语义保持原样
            else:
                return status, (json.loads(body) if body.strip() else {})
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with (opener.open(req, timeout=timeout) if opener is not None
                  else urllib.request.urlopen(req, timeout=timeout)) as resp:
                raw = resp.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise ValueError(f"upstream response exceeds {_MAX_RESPONSE_BYTES} bytes")
                body = raw.decode("utf-8", "replace")
                return int(resp.status), (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            if int(exc.code) == 401:      # key 可能刚被轮换：让缓存失效，下次重读
                self._vsec, self._vsec_ts = None, 0.0
            raise BrainHTTPError(int(exc.code), url, body) from exc
