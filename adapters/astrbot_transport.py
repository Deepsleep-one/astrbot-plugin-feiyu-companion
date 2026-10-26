"""真实发送：把一条 Delivery 交给 AstrBot 的消息管道，真的发到 QQ 上。

这是 M5 里唯一缺的那一厘米。`DisabledTransport` 会如实说「未接线」，这个适配器
才会真的调 `context.send_message(session, MessageChain)`。

四条自保规矩（都是踩过的坑换来的）：
1. **只发私聊**：`GroupMessage` 默认一律拒绝——主动消息在群里说，代价不可撤回；
2. **会话标识必须能解析**：`平台:FriendMessage:号码`，解析不了就不发，不做猜测；
3. **空正文不发**：宁可失败，也不发一条空白消息出去；
4. **回执不撒谎**：`context.send_message` 返回的是「找到没找到平台」，不是「对方收到了」。
   所以 `Receipt.ok` 只代表「已经交给平台」，`detail` 里写明这一点，不吹成已送达。

它不会碰记忆库、不写任何数据库、不开本地 SQL 库。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from ..domain.delivery import Delivery
from ..ports.transport import Receipt
from ..runtime.clock import TZ

MAX_TEXT = 600
"""一条主动消息最多这么长。她要说的是一句话，不是一篇作文。"""

PRIVATE_ONLY = "FriendMessage"
"""默认只允许私聊。想发群聊得显式配置 `allow_group`，并且那是另一个决定。"""


def split_session(session: str) -> tuple[str, str, str] | None:
    """把 `平台:FriendMessage:号码` 拆成三段；拆不出来返回 None。"""
    text = (session or "").strip()
    if not text:
        return None
    parts = text.split(":", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        return None
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


def build_chain(text: str):
    """造一条纯文本消息链。延迟 import，测试时不依赖 AstrBot 本体也能跑。"""
    from astrbot.api.event import MessageChain

    return MessageChain().message(text)


class AstrBotTransport:
    """把 `Delivery.text` 真的发出去。"""

    name = "astrbot"

    def __init__(
        self,
        context: Any,
        *,
        default_session: str = "",
        allow_group: bool = False,
        clock: Any = None,
        segmenter: Any = None,
    ) -> None:
        self.context = context
        self.default_session = (default_session or "").strip()
        self.allow_group = bool(allow_group)
        self.clock = clock
        self.segmenter = segmenter

    def session_for(self, delivery: Delivery) -> str:
        """优先用投递自带的会话，没有就退回默认会话。"""
        return (delivery.session_id or "").strip() or self.default_session

    def check(self, delivery: Delivery) -> str:
        """出站前的自查，返回空串代表可以发。"""
        text = (delivery.text or "").strip()
        if not text:
            return "正文是空的，不发"
        if len(text) > MAX_TEXT:
            return f"正文太长（{len(text)} 字，上限 {MAX_TEXT}）"
        session = self.session_for(delivery)
        if not session:
            return "不知道该发给谁：没有会话标识"
        parts = split_session(session)
        if parts is None:
            return f"会话标识不合法：{session}"
        _, message_type, _ = parts
        if message_type != PRIVATE_ONLY and not self.allow_group:
            return f"主动消息只发给私聊，这条是 {message_type}"
        return ""

    async def send(self, delivery: Delivery) -> Receipt:
        reason = self.check(delivery)
        if reason:
            return Receipt(ok=False, detail=reason)

        session = self.session_for(delivery)
        text = (delivery.text or "").strip()
        sent_at = datetime.now(TZ).isoformat(timespec="seconds")

        plan = self.segmenter.plan(text) if self.segmenter is not None else None
        segments = plan.segments if plan else (text,)
        intervals = plan.intervals if plan else ()
        if not segments:
            return Receipt(ok=False, detail="没有可发的段落")

        sent_count = 0
        for index, segment in enumerate(segments):
            body = str(segment or "").strip()
            if not body:
                continue
            try:
                chain = build_chain(body)
            except Exception as exc:  # noqa: BLE001 - 造链失败只是这一条失败
                return Receipt(ok=False, detail=f"消息链构造失败：{type(exc).__name__}: {exc}")

            try:
                found = await self.context.send_message(session, chain)
            except Exception as exc:  # noqa: BLE001 - 平台异常不能炸掉整批投递
                return Receipt(
                    ok=False,
                    detail=f"发送时抛错（第 {index + 1}/{len(segments)} 段）：{type(exc).__name__}: {exc}",
                )

            if not found:
                return Receipt(
                    ok=False,
                    detail=f"没有匹配 {session} 的平台实例（平台没连上）",
                )
            sent_count += 1

            # 段与段之间按打字速度停一下；最后一段之后不停
            if index < len(intervals):
                pause = float(intervals[index] or 0.0)
                if pause > 0:
                    await asyncio.sleep(pause)

        if sent_count == 0:
            return Receipt(ok=False, detail="分段后没有可发的内容")

        detail = "已交给平台（是否送达以对方是否收到为准）"
        if sent_count > 1:
            detail = f"分 {sent_count} 段发出（{detail}）"
        return Receipt(ok=True, detail=detail, sent_at=sent_at, external_id=session)


def transport_for(
    config: dict,
    context: Any,
    *,
    default_session: str = "",
    clock: Any = None,
    segmenter: Any = None,
):
    """按配置决定用真发还是诚实的假发。"""
    from ..ports.transport import DisabledTransport

    if not config.get("proactive_enable"):
        return DisabledTransport()
    return AstrBotTransport(
        context,
        default_session=default_session or str(config.get("target_session") or ""),
        allow_group=bool(config.get("allow_group")),
        clock=clock,
        segmenter=segmenter,
    )
