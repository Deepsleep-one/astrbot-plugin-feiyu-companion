"""投递账本的落盘实现（M5）。

JSON 文件足够用：账本只在「计划 → 发送」这一刻读写，量很小。
写入走 `写临时文件 + 原子替换`，断电最多丢最后一条，不会写坏整本账。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class JsonFileDeliveryStore:
    """把账本存成一个 JSON 对象：`{idem_key: record}`。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # ---- 读写 -------------------------------------------------------------
    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # 账本坏了宁可当成空账本重来，也不要抛异常卡住整个投递
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- 端口 -------------------------------------------------------------
    def get(self, idem_key: str) -> dict | None:
        row = self._load().get(idem_key)
        return dict(row) if isinstance(row, dict) else None

    def put(self, idem_key: str, record: dict) -> None:
        data = self._load()
        data[idem_key] = dict(record)
        self._save(data)

    def all(self) -> list[dict]:
        return [dict(r) for r in self._load().values() if isinstance(r, dict)]
