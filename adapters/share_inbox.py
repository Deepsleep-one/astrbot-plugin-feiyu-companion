"""分享收件账本（M6-D）：一份「这一条我到底发没发」的本地凭据。

为什么要单独一本账？因为 B站 那边的 outbox 只能记「认领过」，不能记「我在把
消息交给平台的半路上崩了」。那个缝里如果重放，主人会收到两条同样的视频；如果
直接当没发生，就漏了一条。

所以顺序是固定的：**先写 `attempting`，再发，再写结果**。重启后看到一条
`attempting`，就只能当一个 `unknown`：占额度、不重放、写回执。

文件坏了怎么办？**fail closed**：抛 `LedgerCorrupt`，这一轮什么都不发。宁可
停一轮，也不在账本不清不楚的时候把旧东西重放一遍。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

OUTCOME_ATTEMPTING = "attempting"

FIELDS = ("generation", "payload_hash", "bvid", "target", "outcome", "detail", "at")


class LedgerCorrupt(RuntimeError):
    """账本读不动了。宁可这一轮不发，也不要瞎重放。"""


class JsonlShareInbox:
    """一行一次尝试的 JSONL 账本。最后一行说了算。"""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._cache: dict[str, dict] = {}
        self._loaded = False

    # ------------------------------------------------------------------ 读
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerCorrupt(f"账本读不动：{exc}") from exc
        for number, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerCorrupt(f"账本第 {number} 行不是 JSON：{exc}") from exc
            if not isinstance(row, dict):
                raise LedgerCorrupt(f"账本第 {number} 行不是对象")
            generation = str(row.get("generation") or "").strip()
            if not generation:
                raise LedgerCorrupt(f"账本第 {number} 行没有 generation")
            self._cache[generation] = {key: row.get(key) for key in FIELDS}

    def get(self, generation: str) -> dict | None:
        self._load()
        row = self._cache.get((generation or "").strip())
        return dict(row) if row else None

    def attempts(self, generation: str) -> int:
        self._load()
        generation = (generation or "").strip()
        return sum(1 for key in self._cache if key == generation)

    def entries(self, limit: int = 50) -> list[dict]:
        self._load()
        rows = list(self._cache.values())
        rows.sort(key=lambda r: str(r.get("at") or ""))
        return rows[-max(1, int(limit)):]

    def count(self) -> int:
        self._load()
        return len(self._cache)

    def has(self, generation: str) -> bool:
        return self.get(generation) is not None

    # ------------------------------------------------------------------ 写
    def record(self, *, generation: str, payload_hash: str, outcome: str,
               bvid: str = "", target: str = "", detail: str = "", at: str = "") -> None:
        self._load()
        generation = (generation or "").strip()
        if not generation:
            raise ValueError("账本不记没有对账号的东西")
        row = {
            "generation": generation,
            "payload_hash": str(payload_hash or ""),
            "bvid": str(bvid or ""),
            "target": str(target or ""),
            "outcome": str(outcome or ""),
            "detail": str(detail or "")[:200],
            "at": at or datetime.now().isoformat(timespec="seconds"),
        }
        self._cache[generation] = row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:  # pragma: no cover - 有些文件系统不支持
                pass
