"""出站传输端口（M5）。

**这一版没有任何真实传输实现。** 真实发 QQ 消息要等主人单独点头，
在那之前运行环境里装的是 `DisabledTransport`：它只会老老实实报
`integration_pending`，绝不假装发送成功。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.delivery import Delivery


class TransportError(RuntimeError):
    """传输层不可用（未接线、被禁用、平台拒绝）。"""


@dataclass(frozen=True)
class Receipt:
    """一次发送的回执。失败也要有回执，不许只返回 None。"""

    ok: bool
    detail: str = ""
    sent_at: str = ""
    external_id: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "sent_at": self.sent_at,
            "external_id": self.external_id,
        }


@runtime_checkable
class Transport(Protocol):
    name: str

    def send(self, delivery: Delivery) -> Receipt:  # pragma: no cover - 协议
        ...


class DisabledTransport:
    """没接线时的占位：每次发送都如实失败。"""

    name = "disabled"

    def send(self, delivery: Delivery) -> Receipt:
        return Receipt(
            ok=False,
            detail="integration_pending：真实发送尚未接线（需要主人明确授权）",
        )


class FakeTransport:
    """测试用的内存传输：记下发了什么，可指定前 N 次失败。"""

    name = "fake"

    def __init__(self, *, fail_times: int = 0, fail_detail: str = "网络抖动", clock=None) -> None:
        self.fail_times = int(fail_times)
        self.fail_detail = fail_detail
        self.clock = clock
        self.sent: list[Delivery] = []
        self.calls = 0

    def send(self, delivery: Delivery) -> Receipt:
        self.calls += 1
        if self.calls <= self.fail_times:
            return Receipt(ok=False, detail=self.fail_detail)
        self.sent.append(delivery)
        stamp = self.clock.now().isoformat(timespec="seconds") if self.clock else ""
        return Receipt(ok=True, detail="fake", sent_at=stamp, external_id=f"fake:{len(self.sent)}")

    @property
    def sent_texts(self) -> list[str]:
        return [d.text for d in self.sent]
