"""分享交接的端口契约（M6-D）。

B站那边有一张 outbox：它决定「这一条值得分享」，把一条带对账号的票据写进去。
陪伴这边要做的事情只有一件——**认领、发出去、把结果写回去**，而且只能做一次。

这里的 DTO 只是把那一行读出来，白名单收窄成陪伴真正需要的字段。凡是这一层不
认识的字段一律不进；凡是状态不是 `claimed` 的票据一律不发（没被认领的东西不该
被第三方顺手发掉）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

SHARE_BRIDGE_NAME = "BiliSharePortV1"
TICKET_VERSION = 1

STATE_CLAIMED = "claimed"
OUTCOME_SENT = "sent"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_FAILED = "failed"
OUTCOME_REFUSED = "refused"
OUTCOMES = (OUTCOME_SENT, OUTCOME_UNKNOWN, OUTCOME_FAILED, OUTCOME_REFUSED)

TICKET_FIELDS = (
    "generation",
    "scope",
    "bvid",
    "page",
    "revision",
    "target",
    "message",
    "payload_hash",
    "grant_revision",
    "deadline",
    "state",
    "attempts",
)

MAX_MESSAGE = 600
"""一条分享最多这么长。她要说的是一句话，不是一段导语。"""


class ShareBridgeUnavailable(RuntimeError):
    """对端没有提供交接面，或者模式还没打开。"""


class ShareTicketError(ValueError):
    """票据不合格：缺字段、状态不对、正文为空。"""


@dataclass(frozen=True)
class ShareTicket:
    """一条待发的分享。`generation` + `payload_hash` 是它的身份证。"""

    generation: str
    scope: str
    bvid: str
    target: str
    message: str
    payload_hash: str
    page: int = 1
    revision: int = 0
    grant_revision: str = ""
    deadline: str = ""
    state: str = STATE_CLAIMED
    attempts: int = 0

    def __post_init__(self) -> None:
        for name in ("generation", "scope", "bvid", "target", "message", "payload_hash"):
            if not str(getattr(self, name) or "").strip():
                raise ShareTicketError(f"票据缺字段：{name}")

    def to_dict(self) -> dict:
        return {
            "generation": self.generation,
            "scope": self.scope,
            "bvid": self.bvid,
            "target": self.target,
            "message": self.message,
            "payload_hash": self.payload_hash,
            "page": int(self.page or 1),
            "revision": int(self.revision or 0),
            "grant_revision": self.grant_revision,
            "deadline": self.deadline,
            "state": self.state,
            "attempts": int(self.attempts or 0),
        }


@dataclass(frozen=True)
class ShareAck:
    """写回 B站 的结果。`unknown` 表示「交出去了但说不准」，不是「没发」。"""

    generation: str
    outcome: str
    detail: str = ""
    duplicate: bool = False

    def to_dict(self) -> dict:
        return {
            "generation": self.generation,
            "outcome": self.outcome,
            "detail": self.detail,
            "duplicate": self.duplicate,
        }


@dataclass
class RelayReport:
    """一轮交接的账：认领了几条、发了几条、几条说不清。"""

    claimed: int = 0
    sent: int = 0
    unknown: int = 0
    refused: int = 0
    duplicates: int = 0
    recovered: int = 0
    conflicts: int = 0
    # 这一轮有几条真正占用了「今天的主动消息额度」（合账，M6-D2）。
    charged: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "claimed": self.claimed,
            "sent": self.sent,
            "unknown": self.unknown,
            "refused": self.refused,
            "duplicates": self.duplicates,
            "recovered": self.recovered,
            "conflicts": self.conflicts,
            "charged": self.charged,
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        return (
            f"认领 {self.claimed} 条，发出 {self.sent} 条，说不清 {self.unknown} 条，"
            f"放弃 {self.refused} 条，重复 {self.duplicates} 条，冲突 {self.conflicts} 条"
        )


def normalize_ticket(raw: dict, *, delivery_state: str = STATE_CLAIMED) -> ShareTicket:
    """把对端的一行收成票据。只认白名单，只认已认领。"""
    if not isinstance(raw, dict):
        raise ShareTicketError("票据必须是 dict")
    picked = {name: raw.get(name) for name in TICKET_FIELDS}
    state = str(picked.get("state") or "").strip()
    if state != delivery_state:
        raise ShareTicketError(f"票据状态是 {state or '空'}，只有 {delivery_state} 才能发")
    message = str(picked.get("message") or "").strip()
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE]
    return ShareTicket(
        generation=str(picked.get("generation") or "").strip(),
        scope=str(picked.get("scope") or "").strip(),
        bvid=str(picked.get("bvid") or "").strip(),
        target=str(picked.get("target") or "").strip(),
        message=message,
        payload_hash=str(picked.get("payload_hash") or "").strip(),
        page=int(picked.get("page") or 1),
        revision=int(picked.get("revision") or 0),
        grant_revision=str(picked.get("grant_revision") or ""),
        deadline=str(picked.get("deadline") or ""),
        state=state,
        attempts=int(picked.get("attempts") or 0),
    )


@runtime_checkable
class ShareBridge(Protocol):
    """对端的交接面（同步、离线）。"""

    name: str

    def claim(self, *, scope: str = "", limit: int = 1) -> list[ShareTicket]:  # pragma: no cover
        ...

    def ack(self, ticket: ShareTicket, outcome: str, detail: str = "") -> dict:  # pragma: no cover
        ...
