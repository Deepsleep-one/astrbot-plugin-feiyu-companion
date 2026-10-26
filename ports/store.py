"""投递账本的存储端口（M5）。

账本存在的唯一理由：**同一个 `idem_key` 只能成功送出一次**。
进程重启、重复入队、重试，都不能让主人收到两条一样的消息。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DeliveryStore(Protocol):
    def get(self, idem_key: str) -> dict | None:  # pragma: no cover - 协议
        ...

    def put(self, idem_key: str, record: dict) -> None:  # pragma: no cover - 协议
        ...

    def all(self) -> list[dict]:  # pragma: no cover - 协议
        ...


class MemoryDeliveryStore:
    """纯内存账本，进程一退就没了——只给测试和一次性场景用。"""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def get(self, idem_key: str) -> dict | None:
        row = self._rows.get(idem_key)
        return dict(row) if row else None

    def put(self, idem_key: str, record: dict) -> None:
        self._rows[idem_key] = dict(record)

    def all(self) -> list[dict]:
        return [dict(r) for r in self._rows.values()]
