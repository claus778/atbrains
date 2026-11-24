"""有界 JSONL 离线写队列 —— 服务端暂时不可达时本地缓冲，恢复后按序重放。

替代过去「客户端直连数据库降级」的危险模式：
  * 客户端不再需要任何数据库账号/驱动；
  * `/v1/memory/turn` 有游标幂等、`/v1/memory/remember` 有内容哈希防重、
    `claim_remember`/`episode_set` 是槽位/upsert 语义，因此断网恢复重放安全。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable


class OfflineSpool:
    """线程安全的本地 JSONL 离线队列（有最大体积上限，4xx 不可恢复错误进 deadletter）。"""

    def __init__(self, directory: Path | str, *, max_bytes: int = 10 * 1024 * 1024) -> None:
        self.dir = Path(directory).expanduser()
        self.queue_path = self.dir / "queue.jsonl"
        self.dead_path = self.dir / "deadletter.jsonl"
        self.max_bytes = max(4096, int(max_bytes))
        self._lock = threading.Lock()

    def count(self) -> int:
        with self._lock:
            if not self.queue_path.exists():
                return 0
            try:
                return sum(1 for ln in self.queue_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip())
            except OSError:
                return 0

    def size_bytes(self) -> int:
        with self._lock:
            try:
                return self.queue_path.stat().st_size
            except OSError:
                return 0

    def enqueue(self, path: str, payload: dict[str, Any]) -> bool:
        entry = {"ts": round(time.time(), 3), "path": str(path), "payload": payload}
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            cur_size = 0
            try:
                cur_size = self.queue_path.stat().st_size
            except OSError:
                cur_size = 0
            if cur_size + len(encoded) > self.max_bytes:
                return False
            fd = os.open(self.queue_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab") as f:
                f.write(encoded)
            return True

    def _rewrite_queue(self, lines: list[str]) -> None:
        if not lines:
            self.queue_path.unlink(missing_ok=True)
            return
        tmp = self.queue_path.with_name("queue.jsonl.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")
        os.replace(tmp, self.queue_path)

    def _append_deadletter(self, line: str, reason: str) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            rec = json.dumps({"dead_ts": round(time.time(), 3), "reason": reason[:200], "raw": line},
                             ensure_ascii=False) + "\n"
            fd = os.open(self.dead_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(rec)
        except OSError:
            pass

    def drain(self, sender: Callable[[str, dict[str, Any]], Any], *,
              is_transient: Callable[[BaseException], bool],
              max_items: int = 100) -> dict[str, int]:
        """按 FIFO 重放队列；遇到瞬时故障立即停止并保留剩余条目。"""
        stats = {"sent": 0, "dropped": 0, "remaining": 0}
        with self._lock:
            if not self.queue_path.exists():
                return stats
            raw_lines = [
                ln for ln in self.queue_path.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip()
            ]
            if not raw_lines:
                self.queue_path.unlink(missing_ok=True)
                return stats

            idx = 0
            limit = max(1, int(max_items))
            while idx < len(raw_lines) and stats["sent"] + stats["dropped"] < limit:
                ln = raw_lines[idx]
                try:
                    item = json.loads(ln)
                    path = str(item["path"])
                    payload = item["payload"]
                    if not isinstance(payload, dict):
                        raise ValueError("payload must be object")
                except Exception as exc:  # noqa: BLE001
                    self._append_deadletter(ln, f"corrupt: {exc}")
                    stats["dropped"] += 1
                    idx += 1
                    continue

                try:
                    sender(path, payload)
                    stats["sent"] += 1
                    idx += 1
                except BaseException as exc:  # noqa: BLE001
                    if is_transient(exc):
                        break
                    self._append_deadletter(ln, str(exc))
                    stats["dropped"] += 1
                    idx += 1

            remaining = raw_lines[idx:]
            self._rewrite_queue(remaining)
            stats["remaining"] = len(remaining)
            return stats
