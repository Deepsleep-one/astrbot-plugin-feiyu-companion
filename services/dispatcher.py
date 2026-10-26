"""投递队列与状态机（M5）。

它做的只有一件事：把 M4 算出来的 `pending` 计划，变成**最多成功一次**的出站。

四条硬规矩：
1. 同一个 `idem_key` 只成功一次（账本落盘，重启也不重复）；
2. 状态只能按 `TRANSITIONS` 往前走，非法迁移直接抛；
3. 失败算进预算的退避，重试用尽就 `dropped` 并写明原因；
4. 语音没有单轮授权，在出站前就被拦下（`ensure_deliverable`）。

**这一版只认 `FakeTransport` / `DisabledTransport`**：真实 QQ 发送没有接线，
`DisabledTransport` 会让每条都如实失败，绝不假装发出去了。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import timedelta

from ..domain.delivery import (
    STATUS_DROPPED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENDING,
    STATUS_SENT,
    Delivery,
    ensure_deliverable,
)
from ..domain.context import SCENE_PRIVATE
from ..domain.grants import CAP_TEXT, CapabilityDenied, CapabilityGrant
from ..ports.transport import Receipt, Transport, TransportError
from ..runtime.clock import Clock, SystemClock
from .budget import Budget

MAX_ATTEMPTS = 3

TEXT_ONLY = CapabilityGrant(frozenset({CAP_TEXT}), SCENE_PRIVATE, reason="默认只发文字")


class DispatchError(RuntimeError):
    """投递流程里不该发生的事。"""


@dataclass
class DispatchReport:
    sent: int = 0
    failed: int = 0
    dropped: int = 0
    skipped: int = 0
    attempts: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sent": self.sent,
            "failed": self.failed,
            "dropped": self.dropped,
            "skipped": self.skipped,
            "attempts": self.attempts,
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        return (f"发 {self.sent} 条，失败 {self.failed} 条，"
                f"放弃 {self.dropped} 条，跳过 {self.skipped} 条")


class DeliveryQueue:
    def __init__(
        self,
        store,
        *,
        clock: Clock | None = None,
        budget: Budget | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.store = store
        self.clock = clock or SystemClock()
        self.budget = budget
        self.max_attempts = max(1, int(max_attempts))
        self._queue: dict[str, Delivery] = {}

    # ---- 入队 -------------------------------------------------------------
    def enqueue(self, delivery: Delivery) -> Delivery:
        key = delivery.idem_key or delivery.id
        if not delivery.idem_key:
            delivery.idem_key = key
        known = self._queue.get(key)
        if known is not None:
            return known
        row = self.store.get(key)
        if row is not None:
            restored = Delivery.from_dict(row.get("delivery") or {})
            restored.status = str(row.get("status") or restored.status)
            if restored.status == STATUS_SENDING:
                # 账本停在 sending = 上次进程在「已经交给传输层」那一刻没了。
                # 对方到底收没收到，我们无从知道；宁可少发一次，也不发第二遍（M10-C 演练定下的规矩）。
                restored.move(STATUS_DROPPED, reason="上次发送中途崩过，不确定对方收没收到：不重放")
                restored.extra = dict(restored.extra, duplicate_of_sent=False, unknown=True)
                self._queue[key] = restored
                self._save(key, restored, "")
                return restored
            restored.extra = dict(restored.extra, duplicate_of_sent=restored.status == STATUS_SENT)
            self._queue[key] = restored
            return restored
        self._queue[key] = delivery
        self._save(key, delivery, "")
        return delivery

    # ---- 查询 -------------------------------------------------------------
    def get(self, idem_key: str) -> Delivery | None:
        return self._queue.get(idem_key)

    def pending(self) -> list[Delivery]:
        return [d for d in self._queue.values() if not d.is_terminal]

    def snapshot(self) -> dict:
        counts: dict[str, int] = {}
        for d in self._queue.values():
            counts[d.status] = counts.get(d.status, 0) + 1
        return {
            "total": len(self._queue),
            "by_status": counts,
            "attempts": sum(d.attempts for d in self._queue.values()),
        }

    # ---- 发送 -------------------------------------------------------------
    def dispatch_all(
        self,
        transport: Transport,
        *,
        now=None,
        grants: dict[str, CapabilityGrant] | None = None,
    ) -> DispatchReport:
        """同步版：给 `FakeTransport` / `DisabledTransport` 与离线测试用。"""
        moment = now or self.clock.now()
        report = DispatchReport()
        for key, delivery in self._targets(moment, report, grants):
            self._begin(delivery, report)
            self._finish(key, delivery, self._send(transport, delivery), moment, report)
        return report

    async def dispatch_all_async(
        self,
        transport,
        *,
        now=None,
        grants: dict[str, CapabilityGrant] | None = None,
    ) -> DispatchReport:
        """异步版：给 `AstrBotTransport` 用（它的 `send` 是协程）。

        同步传输层也能用这个版本——`_send_async` 会先看返回的是不是可等待对象。
        """
        moment = now or self.clock.now()
        report = DispatchReport()
        for key, delivery in self._targets(moment, report, grants):
            self._begin(delivery, report)
            receipt = await self._send_async(transport, delivery)
            self._finish(key, delivery, receipt, moment, report)
        return report

    # ---- 内部 -------------------------------------------------------------
    def _targets(self, moment, report: DispatchReport, grants):
        """产出可以尝试发送的条目；被拦下或跳过的就地记账。"""
        for key, delivery in list(self._queue.items()):
            if delivery.is_terminal:
                report.skipped += 1
                continue
            if delivery.next_retry_at and delivery.next_retry_at > moment.isoformat(timespec="seconds"):
                report.skipped += 1
                continue
            if delivery.attempts >= self.max_attempts:
                delivery.move(STATUS_DROPPED, reason=f"重试 {delivery.attempts} 次仍失败，不再试")
                self._save(key, delivery, "")
                report.dropped += 1
                continue

            grant = (grants or {}).get(delivery.session_id) or TEXT_ONLY
            try:
                ensure_deliverable(delivery, grant)
            except CapabilityDenied as exc:
                delivery.move(STATUS_DROPPED, reason=f"没有授权：{exc}")
                self._save(key, delivery, "")
                report.dropped += 1
                report.notes.append(f"{delivery.id} 被授权闸拦下：{exc}")
                continue

            yield key, delivery

    def _begin(self, delivery: Delivery, report: DispatchReport) -> None:
        delivery.attempts += 1
        report.attempts += 1
        delivery.move(STATUS_SENDING)
        # **先落盘再发**：这样「发送中途崩掉」在账本上留下的是 sending，
        # 重启时 `enqueue` 会把它判成「不确定、不重放」，而不是当成 pending 再发一遍。
        self._save(delivery.idem_key or delivery.id, delivery, "")

    def _finish(self, key: str, delivery: Delivery, receipt: Receipt, moment, report: DispatchReport) -> None:
        if receipt.ok:
            delivery.move(STATUS_SENT)
            delivery.last_error = ""
            delivery.next_retry_at = ""
            if self.budget is not None:
                self.budget.record_success()
            self._save(key, delivery, receipt.external_id)
            report.sent += 1
            return

        delivery.last_error = receipt.detail or "未知失败"
        wait = self.budget.record_failure() if self.budget is not None else 30.0
        retry_at = (moment + timedelta(seconds=wait)).isoformat(timespec="seconds")
        if delivery.attempts >= self.max_attempts:
            delivery.move(STATUS_DROPPED, reason=f"发送失败且重试用尽：{delivery.last_error}")
            report.dropped += 1
        else:
            delivery.move(STATUS_FAILED, reason=f"发送失败：{delivery.last_error}")
            delivery.next_retry_at = retry_at
            report.failed += 1
        self._save(key, delivery, "")

    def _send(self, transport: Transport, delivery: Delivery) -> Receipt:
        try:
            receipt = transport.send(delivery)
        except TransportError as exc:
            return Receipt(ok=False, detail=f"传输层不可用：{exc}")
        except Exception as exc:  # noqa: BLE001 - 任何异常都算一次失败，不许冒泡打断整批
            return Receipt(ok=False, detail=f"{type(exc).__name__}: {exc}")
        if receipt is None:
            return Receipt(ok=False, detail="传输层没有返回回执")
        return receipt

    async def _send_async(self, transport, delivery: Delivery) -> Receipt:
        try:
            receipt = transport.send(delivery)
            if inspect.isawaitable(receipt):
                receipt = await receipt
        except TransportError as exc:
            return Receipt(ok=False, detail=f"传输层不可用：{exc}")
        except Exception as exc:  # noqa: BLE001
            return Receipt(ok=False, detail=f"{type(exc).__name__}: {exc}")
        if receipt is None:
            return Receipt(ok=False, detail="传输层没有返回回执")
        return receipt

    def _save(self, key: str, delivery: Delivery, external_id: str) -> None:
        self.store.put(key, {
            "idem_key": key,
            "status": delivery.status,
            "delivery": delivery.to_dict(),
            "external_id": external_id,
            "updated_at": self.clock.now().isoformat(timespec="seconds"),
        })
