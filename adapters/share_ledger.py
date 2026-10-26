"""分享影子账本（M6-C）。

一行一个 generation 的 JSONL，**只记判断，不记发送**——因为这一版根本没有发送。

为什么要落盘而不是只打日志：
- 将来真交接时，「这个 generation 我有没有看过」就是对账的第一手证据；
- 同一天同一份材料重复评估，generation 相同，账本里只会留一行（幂等）；
- 坏了当空账本（宁可少一条历史，也不要卡住诊断）。

写入用追加；追加失败不抛出，只返回 False——诊断链路不该因为磁盘问题炸掉。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..ports.bili_share import MODE_SHADOW, OP_SEND, ShareReport


class JsonlShareLedger:
    """按 generation 去重的 JSONL 账本。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # ---- 读 ---------------------------------------------------------------
    def entries(self, *, limit: int = 50) -> list[dict]:
        rows: list[dict] = []
        if not self.path.exists():
            return rows
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return rows
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue  # 坏行跳过，不整本作废
            if isinstance(item, dict):
                rows.append(item)
        rows.sort(key=lambda row: str(row.get("created_at") or ""))
        return rows[-limit:] if limit > 0 else rows

    def generations(self) -> set[str]:
        return {str(row.get("generation") or "") for row in self.entries(limit=0)} - {""}

    def has(self, generation: str) -> bool:
        return str(generation or "") in self.generations()

    def count(self) -> int:
        return len(self.entries(limit=0))

    def last_share_at(self) -> datetime | None:
        """上一次「影子判定会发」的时刻（不是真发过）。"""
        for row in reversed(self.entries(limit=0)):
            decisions = row.get("decisions") or []
            if any(isinstance(d, dict) and d.get("op") == OP_SEND for d in decisions):
                stamp = str(row.get("created_at") or "")
                if stamp:
                    try:
                        return datetime.fromisoformat(stamp)
                    except ValueError:
                        continue
        return None

    # ---- 写 ---------------------------------------------------------------
    def record(self, report: ShareReport) -> bool:
        """记一轮影子判断；同 generation 已存在则返回 False（不重复记账）。"""
        generation = str(getattr(report, "generation", "") or "").strip()
        if not generation:
            return False
        if self.has(generation):
            return False
        payload = {
            "generation": generation,
            "created_at": str(getattr(report, "created_at", "") or ""),
            "mode": str(getattr(report, "mode", MODE_SHADOW) or MODE_SHADOW),
            "counts": report.counts(),
            "decisions": [
                {
                    "bvid": item.bvid,
                    "page": int(item.page),
                    "op": item.op,
                    "reason": item.reason,
                }
                for item in report.decisions
            ],
            "notes": list(report.notes),
            "executable": False,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            return False
        return True
