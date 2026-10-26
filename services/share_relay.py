"""分享转交（M6-D）：唯一的发送端在这里，而且一辈子只发一次。

一轮的流程就四步，顺序不能换：

1. `bridge.claim()` 认领票据——认领本身就是原子的，别人拿不到同一条；
2. 先看本地账本：这条发过没有？载荷还是不是同一份？
   - 发过 → 直接把旧结果写回去，不再发；
   - 载荷变了 → 拒发并写回执（同一对账号不可能有两个内容）；
   - 上次卡在 `attempting` → 这次只记 `unknown`，也不发；
3. 真发：交给 `Transport`。成功 `sent`；只交到平台、说不清 → `unknown`；
   传输层压根没接线（`integration_pending`）→ `refused`，这一条不占额度；
4. 把结果写回 B站 的 outbox。写不回去也没关系：票据会在认领超时后回到 pending，
   而本地账本记得这条发过，下一轮只会补一次回执、不会再发一遍。
"""

from __future__ import annotations

import asyncio
import inspect

from ..domain.delivery import KIND_TEXT, Delivery
from ..ports.bili_share_outbox import (
    OUTCOME_REFUSED,
    OUTCOME_SENT,
    OUTCOME_UNKNOWN,
    RelayReport,
    ShareBridgeUnavailable,
    ShareTicket,
)
from ..ports.transport import Receipt
from ..adapters.share_inbox import OUTCOME_ATTEMPTING, JsonlShareInbox, LedgerCorrupt

PENDING_DETAIL = "integration_pending"


class ShareRelay:
    """把 B站 认领到的分享交给传输层，并把结果写回去。"""

    name = "share_relay"

    def __init__(self, bridge, inbox: JsonlShareInbox, transport, *,
                 clock=None, max_per_round: int = 1, budget=None,
                 count_against_budget: bool = True) -> None:
        self.bridge = bridge
        self.inbox = inbox
        self.transport = transport
        self.clock = clock
        self.max_per_round = max(1, int(max_per_round or 1))
        # 合账（M6-D2）：发出去的一条分享也占一条「今天的主动消息」。
        # budget 为 None 时只记账不扣额度——默认没有预算本就不该假装有。
        self.budget = budget
        self.count_against_budget = bool(count_against_budget)

    # ------------------------------------------------------------------ 工具
    def _now(self) -> str:
        if self.clock is not None:
            return self.clock.now().isoformat(timespec="seconds")
        return ""

    def _ack(self, ticket: ShareTicket, outcome: str, detail: str) -> bool:
        """写回执；写不回去就记下来，不让这一轮整批炸掉。"""
        try:
            self.bridge.ack(ticket, outcome, detail)
            return True
        except ShareBridgeUnavailable:
            return False

    # ------------------------------------------------------------------ 主流程
    def run_once(self, *, scope: str = "", now=None) -> RelayReport:
        """同步入口（测试、旧的只读演练用）。

        在事件循环里跑会直接报错——那种场合必须 `await run_once_async(...)`，
        否则 `asyncio.run` 会炸在「已经有循环在跑」上。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run_once_async(scope=scope, now=now))
        raise RuntimeError("已经在事件循环里：请用 await relay.run_once_async(...)")

    async def run_once_async(self, *, scope: str = "", now=None) -> RelayReport:
        report = RelayReport()
        try:
            tickets = self.bridge.claim(scope=scope, limit=self.max_per_round)
        except ShareBridgeUnavailable as exc:
            report.notes.append(f"没交接成：{exc}")
            return report
        report.claimed = len(tickets)
        for ticket in tickets:
            try:
                await self._deliver_one(ticket, report)
            except LedgerCorrupt as exc:
                # 账本不清不楚：这一轮全停，票据原样留在对端等超时。
                report.notes.append(f"账本坏了，这轮不发：{exc}")
                return report
        return report

    async def _deliver_one(self, ticket: ShareTicket, report: RelayReport) -> None:
        try:
            entry = self.inbox.get(ticket.generation)
        except LedgerCorrupt:
            raise

        if entry is not None:
            recorded = str(entry.get("payload_hash") or "")
            if recorded and recorded != ticket.payload_hash:
                self.inbox.record(
                    generation=ticket.generation, payload_hash=ticket.payload_hash,
                    outcome=OUTCOME_REFUSED, bvid=ticket.bvid, target=ticket.target,
                    detail="同一对账号出现了另一份载荷", at=self._now(),
                )
                self._ack(ticket, OUTCOME_REFUSED, "对账号与载荷对不上，拒绝发送")
                report.conflicts += 1
                report.notes.append(f"{ticket.generation}：载荷冲突，拒发")
                return
            previous = str(entry.get("outcome") or "")
            if previous == OUTCOME_ATTEMPTING:
                self.inbox.record(
                    generation=ticket.generation, payload_hash=ticket.payload_hash,
                    outcome=OUTCOME_UNKNOWN, bvid=ticket.bvid, target=ticket.target,
                    detail="上次发到一半就断了，不知道发没发出去", at=self._now(),
                )
                self._ack(ticket, OUTCOME_UNKNOWN, "上一轮中断，按 unknown 收口，不重放")
                report.recovered += 1
                report.unknown += 1
                report.notes.append(f"{ticket.generation}：上次中断，按 unknown 收口")
                return
            if previous in (OUTCOME_SENT, OUTCOME_UNKNOWN, OUTCOME_REFUSED):
                self._ack(ticket, previous, str(entry.get("detail") or "重复认领"))
                report.duplicates += 1
                return
            # 其它历史（failed 之类）：这一条已经有过结论，同样不重放。
            self._ack(ticket, previous or OUTCOME_UNKNOWN, "这一条已经有结论，不重放")
            report.duplicates += 1
            return

        # 第一次见：先落 attempting，再发。崩在中间也不会变成「没发过」。
        self.inbox.record(
            generation=ticket.generation, payload_hash=ticket.payload_hash,
            outcome=OUTCOME_ATTEMPTING, bvid=ticket.bvid, target=ticket.target,
            detail="准备发送", at=self._now(),
        )
        receipt = await self._send(ticket)
        outcome, detail = self._classify(receipt)
        self.inbox.record(
            generation=ticket.generation, payload_hash=ticket.payload_hash,
            outcome=outcome, bvid=ticket.bvid, target=ticket.target,
            detail=detail, at=self._now(),
        )
        self._ack(ticket, outcome, detail)
        if outcome == OUTCOME_SENT:
            report.sent += 1
            if self._charge():
                report.charged += 1
        elif outcome == OUTCOME_REFUSED:
            report.refused += 1
        else:
            report.unknown += 1

    def _charge(self) -> bool:
        """发出去一条就记一条账（合账）。

        只给「确认发出去」的记账：说不太清的那条（unknown）在 B站 那边已经由
        `reserve()` 占过额度了，这边再扣一次等于扣两回。
        """
        if not self.count_against_budget or self.budget is None:
            return False
        try:
            self.budget.charge_share()
            return True
        except Exception:
            # 记账失败不该让「已经发出去的这一条」变成没发生。
            return False

    # ------------------------------------------------------------------ 发送
    async def _send(self, ticket: ShareTicket) -> Receipt:
        """真的发一次。同步、异步传输层都能接：awaitable 就等它。"""
        delivery = Delivery(
            id=f"share:{ticket.generation}",
            session_id=ticket.target,
            kind=KIND_TEXT,
            text=ticket.message,
        )
        try:
            receipt = self.transport.send(delivery)
            if inspect.isawaitable(receipt):
                receipt = await receipt
        except Exception as exc:
            return Receipt(ok=False, detail=f"发送抛了 {type(exc).__name__}：{exc}"[:200])
        if receipt is None:
            return Receipt(ok=False, detail="传输层没有返回回执")
        return receipt

    @staticmethod
    def _classify(receipt: Receipt) -> tuple[str, str]:
        detail = str(receipt.detail or "")[:200]
        if receipt.ok:
            return OUTCOME_SENT, detail or "已交给平台"
        if PENDING_DETAIL in detail:
            return OUTCOME_REFUSED, detail or "传输层没接线"
        return OUTCOME_UNKNOWN, detail or "说不清发没发出去"
