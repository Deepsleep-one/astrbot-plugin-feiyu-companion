"""投递契约（M3 定契约，M5 接状态机）。

这里钉住三件事：
1. 默认是文字；
2. 语音必须拿到「单轮」授权才允许出站（主人当次要求才算数）；
3. 状态只能往前走：`pending → sending → sent / failed → dropped`，走过的路不回头。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .grants import CAP_VOICE, CapabilityDenied, CapabilityGrant

KIND_TEXT = "text"
KIND_VOICE = "voice"

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_DROPPED = "dropped"
STATUSES = (STATUS_PENDING, STATUS_SENDING, STATUS_SENT, STATUS_FAILED, STATUS_DROPPED)

#: 允许的状态迁移。`failed` 还能重试回 `sending`；`sent` 与 `dropped` 是终态。
#: `sending → dropped` 是「这次发失败了，且不再重试」——不给这条路就只能先落 failed 再丢，
#: 多一次无意义的状态写入。
TRANSITIONS = {
    STATUS_PENDING: (STATUS_SENDING, STATUS_DROPPED),
    STATUS_SENDING: (STATUS_SENT, STATUS_FAILED, STATUS_DROPPED),
    STATUS_FAILED: (STATUS_SENDING, STATUS_DROPPED),
    STATUS_SENT: (),
    STATUS_DROPPED: (),
}


class DeliveryError(RuntimeError):
    """投递状态不合法。"""


@dataclass
class Delivery:
    id: str
    session_id: str
    kind: str = KIND_TEXT
    text: str = ""
    status: str = STATUS_PENDING
    attempts: int = 0
    due_at: str = ""
    reason: str = ""
    idem_key: str = ""
    created_at: str = ""
    last_error: str = ""
    next_retry_at: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "kind": self.kind,
            "text": self.text,
            "status": self.status,
            "attempts": self.attempts,
            "due_at": self.due_at,
            "reason": self.reason,
            "idem_key": self.idem_key,
            "created_at": self.created_at,
            "last_error": self.last_error,
            "next_retry_at": self.next_retry_at,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Delivery":
        return cls(
            id=str(data.get("id", "")),
            session_id=str(data.get("session_id", "")),
            kind=str(data.get("kind", KIND_TEXT)),
            text=str(data.get("text", "")),
            status=str(data.get("status", STATUS_PENDING)),
            attempts=int(data.get("attempts", 0) or 0),
            due_at=str(data.get("due_at", "")),
            reason=str(data.get("reason", "")),
            idem_key=str(data.get("idem_key", "")),
            created_at=str(data.get("created_at", "")),
            last_error=str(data.get("last_error", "")),
            next_retry_at=str(data.get("next_retry_at", "")),
            extra=dict(data.get("extra") or {}),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in (STATUS_SENT, STATUS_DROPPED)

    def move(self, status: str, *, reason: str = "") -> None:
        """只允许按 `TRANSITIONS` 走；非法迁移直接抛，不静默改。"""
        if status not in STATUSES:
            raise DeliveryError(f"未知投递状态：{status}")
        if status == self.status:
            raise DeliveryError(f"投递 {self.id} 已经是 {status}，不用再改")
        allowed = TRANSITIONS.get(self.status, ())
        if status not in allowed:
            raise DeliveryError(f"投递 {self.id} 不能从 {self.status} 变成 {status}")
        self.status = status
        if reason:
            self.reason = reason


def ensure_deliverable(delivery: Delivery, grant: CapabilityGrant) -> None:
    """出站前的最后一道闸：语音没有单轮授权就不许走。"""
    if delivery.kind == KIND_TEXT:
        return
    if delivery.kind != KIND_VOICE:
        raise CapabilityDenied(f"未知投递类型：{delivery.kind}")
    if not grant.allows(CAP_VOICE):
        raise CapabilityDenied("这条语音没有授权：默认只发文字")
    if not grant.single_turn:
        raise CapabilityDenied("语音必须是一次性的单轮授权，不延续到后续对话")
