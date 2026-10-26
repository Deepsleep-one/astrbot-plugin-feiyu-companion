"""单执行端锁（M5）。

同一个真实账号只允许一个进程持有「主动投递权」。
两个实例同时跑 = 主人可能收到两条一样的消息，所以这件事必须在代码里挡住，
而不是靠「记得只开一个」。

实现是文件锁：`O_CREAT|O_EXCL` 创建成功即为持有者；持有者信息带时间戳，
超过 `ttl_seconds` 视为陈旧锁（进程崩了留下的），允许接管。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .clock import Clock, SystemClock


class LockHeld(RuntimeError):
    """已经被别的执行端拿着了。"""


class ExecutorLock:
    def __init__(self, path: str | Path, *, ttl_seconds: float = 900.0, clock: Clock | None = None) -> None:
        self.path = Path(path)
        self.ttl_seconds = float(ttl_seconds)
        self.clock = clock or SystemClock()
        self.owner = ""
        self.took_over = False
        self.reason = ""

    # ---- 内部 -------------------------------------------------------------
    def holder(self) -> dict | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not raw.strip():
            return None
        try:
            info = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return info if isinstance(info, dict) else None

    def _stale(self, info: dict | None) -> bool:
        if not info:
            return True
        try:
            acquired = info.get("acquired_at", "")
            from datetime import datetime

            when = datetime.fromisoformat(acquired)
        except (ValueError, TypeError):
            return True
        age = (self.clock.now() - when).total_seconds()
        return age > self.ttl_seconds

    def _write(self, info: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- 对外 -------------------------------------------------------------
    def acquire(self, owner: str) -> bool:
        info = {
            "owner": owner,
            "pid": os.getpid(),
            "acquired_at": self.clock.now().isoformat(timespec="seconds"),
        }
        self.took_over = False
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            current = self.holder()
            if not self._stale(current):
                self.reason = f"已有执行端在跑：{(current or {}).get('owner', '未知')}"
                return False
            self._write(info)
            self.owner = owner
            self.took_over = True
            self.reason = "接管了陈旧的执行端锁"
            return True
        except OSError as exc:  # 目录不可写之类
            self.reason = f"拿不到执行端锁：{exc}"
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(info, ensure_ascii=False, indent=2))
        self.owner = owner
        self.reason = ""
        return True

    def release(self) -> None:
        if not self.owner:
            return
        current = self.holder()
        if current and current.get("owner") == self.owner:
            try:
                self.path.unlink()
            except OSError:
                pass
        self.owner = ""

    def __enter__(self) -> "ExecutorLock":
        if not self.acquire(self.owner or f"pid-{os.getpid()}"):
            raise LockHeld(self.reason)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
