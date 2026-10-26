"""M7-A：一次「只这一句」的语音回合。

三件事按顺序发生，任何一步不成立就退回文字：
1. 场景与身份（私聊 + 绑定主人）；
2. 单轮授权（`grant_once(CAP_VOICE)`，不延续到下一轮，也不由模型自己决定）；
3. 中文体检（`ports.voice.prepare`）——不过就只发文字。

这个模块不碰 AstrBot、不碰网络、不读凭证：合成交给 `VoicePort`，发送交给投递链路。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any

from ..domain.delivery import KIND_VOICE, Delivery, DeliveryError, ensure_deliverable
from ..domain.grants import CAP_VOICE, CapabilityDenied, CapabilityGrant, PermissionPolicy
from ..domain.grants import DEFAULT_POLICY
from ..domain.context import InteractionContext
from ..ports.voice import DEFAULT_MAX_CHARS, VoiceRequest, VoiceResult, prepare


@dataclass(frozen=True)
class VoicePlan:
    """计划：能不能念、念什么、走哪条会话。`ok=False` 时 `delivery` 一定是 None。"""

    ok: bool
    reason: str = ""
    text: str = ""
    delivery: Delivery | None = None
    grant: CapabilityGrant | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "text": self.text,
            "delivery": self.delivery.to_dict() if self.delivery else None,
            "grant": self.grant.to_dict() if self.grant else None,
        }


@dataclass(frozen=True)
class VoiceOutcome:
    """结果：念出来了就带上音频路径；没念成就把正文留下当退路。"""

    ok: bool
    reason: str = ""
    delivery: Delivery | None = None
    audio_path: str = ""
    duration_ms: int = 0
    fallback_text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_fallback(self) -> bool:
        return not self.ok and bool(self.fallback_text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "delivery": self.delivery.to_dict() if self.delivery else None,
            "audio_path": self.audio_path,
            "duration_ms": self.duration_ms,
            "fallback_text": self.fallback_text,
            "extra": dict(self.extra),
        }


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


class VoiceTurn:
    """把「主人这一句要语音」翻成一次可执行的投递计划。"""

    def __init__(
        self,
        policy: PermissionPolicy | None = None,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
        clock: Any = None,
    ) -> None:
        self.policy = policy or DEFAULT_POLICY
        self.max_chars = int(max_chars or DEFAULT_MAX_CHARS)
        self.clock = clock

    # ---- 计划 ---------------------------------------------------------
    def plan(self, ctx: InteractionContext, text: str, *, now: str = "") -> VoicePlan:
        if not isinstance(ctx, InteractionContext):
            return VoicePlan(False, "没有会话上下文，不能决定要不要出声")
        if not ctx.is_private:
            return VoicePlan(False, "语音只在私聊里：群里一律只发文字")
        if not ctx.is_owner:
            return VoicePlan(False, "只有主人能让她出声")
        if not ctx.is_bound:
            return VoicePlan(False, "只有绑定主人能让她出声（未绑定时只发文字）")
        body, why = prepare(text, max_chars=self.max_chars)
        if not body:
            return VoicePlan(False, why)
        try:
            grant = self.policy.grant_once(CAP_VOICE, scene=ctx.scene, reason="主人这一句明确要语音")
        except CapabilityDenied as exc:
            return VoicePlan(False, str(exc))
        delivery = Delivery(
            id=f"voice:{_digest(f'{ctx.session_id}|{body}')}",
            session_id=ctx.session_id,
            kind=KIND_VOICE,
            text=body,
            created_at=str(now or ""),
            idem_key=f"voice:{_digest(body)}",
        )
        try:
            ensure_deliverable(delivery, grant)
        except (CapabilityDenied, DeliveryError) as exc:
            return VoicePlan(False, str(exc))
        return VoicePlan(True, "这一次可以出声", body, delivery, grant)

    # ---- 执行 ---------------------------------------------------------
    async def render(self, port: Any, ctx: InteractionContext, text: str, *, now: str = "") -> VoiceOutcome:
        plan = self.plan(ctx, text, now=now)
        if not plan.ok:
            return VoiceOutcome(False, plan.reason, fallback_text=plan.text)
        delivery = plan.delivery
        request = VoiceRequest(
            text=plan.text,
            scene=ctx.scene,
            max_chars=self.max_chars,
        )
        try:
            result = await port.synthesize(request)
        except Exception as exc:  # 端口自己应当不抛；真抛了也只退回文字
            return VoiceOutcome(
                False,
                f"语音端口抛错（{type(exc).__name__}），这条退回文字",
                delivery=delivery,
                fallback_text=plan.text,
            )
        if not isinstance(result, VoiceResult):
            return VoiceOutcome(False, "语音端口返回了看不懂的东西，这条退回文字", fallback_text=plan.text)
        if not result.ok:
            return VoiceOutcome(False, result.reason, delivery=delivery, fallback_text=plan.text)
        sent = replace(delivery, extra={**dict(delivery.extra), "audio_path": result.audio_path})
        return VoiceOutcome(
            True,
            "已经合成好了",
            delivery=sent,
            audio_path=result.audio_path,
            duration_ms=int(result.duration_ms or 0),
            extra={"provider": result.provider},
        )


__all__ = ["VoicePlan", "VoiceOutcome", "VoiceTurn"]
