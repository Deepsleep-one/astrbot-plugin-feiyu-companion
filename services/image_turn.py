"""一次出图请求：计划 → 执行 → 永远有文字兜底。

这里管的是「能不能画」和「画不画得出来」，不管怎么发（发图走 main.py）。
三条规则：
1. 只在私聊、只在主人这轮明确要图时给一次性授权（`CAP_IMAGE`），不延续到下一轮。
2. 有每日张数上限（默认 3），计数是**进程内按天**的——重启会清零，它不是配额账本。
3. 任何一步不成都退回文字：没有授权、连不上、超时、空图，全部如实说原因。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from ..domain.context import InteractionContext
from ..domain.delivery import KIND_TEXT, Delivery, STATUS_PENDING, ensure_deliverable
from ..domain.grants import CAP_IMAGE, CapabilityDenied, CapabilityGrant, PermissionPolicy
from ..ports.images import (
    DEFAULT_MAX_CHARS,
    ImageContractError,
    ImageRequest,
    ImageResult,
    ImageUnavailable,
    prepare_prompt,
)
from ..runtime.clock import Clock, SystemClock, TZ

DEFAULT_DAILY_LIMIT = 3


@dataclass(frozen=True)
class ImagePlan:
    ok: bool
    reason: str = ""
    prompt: str = ""
    delivery: Delivery | None = None
    grant: CapabilityGrant | None = None


@dataclass(frozen=True)
class ImageOutcome:
    ok: bool
    reason: str = ""
    path: str = ""
    elapsed_ms: int = 0
    fallback_text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _digest(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:16]


class ImageTurn:
    def __init__(
        self,
        policy: PermissionPolicy | None = None,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        clock: Clock | None = None,
    ) -> None:
        self.policy = policy or PermissionPolicy()
        self.max_chars = max(1, int(max_chars or DEFAULT_MAX_CHARS))
        self.daily_limit = max(0, int(daily_limit))
        self.clock = clock or SystemClock()
        self._day: date | None = None
        self._used = 0

    # ---- 计数 ---------------------------------------------------------
    def _roll(self) -> None:
        today = self.clock.now().astimezone(TZ).date()
        if self._day != today:
            self._day = today
            self._used = 0

    def used_today(self) -> int:
        self._roll()
        return self._used

    def remaining_today(self) -> int:
        return max(0, self.daily_limit - self.used_today())

    # ---- 计划 ---------------------------------------------------------
    def plan(self, ctx: InteractionContext, text: str, *, now: datetime | None = None) -> ImagePlan:
        if ctx is None:
            return ImagePlan(False, "没有会话上下文")
        if not ctx.is_private:
            return ImagePlan(False, "出图只在私聊里：群里不画")
        if not ctx.is_owner:
            return ImagePlan(False, "只有主人能要图")
        if not ctx.is_bound:
            return ImagePlan(False, "这个会话还没绑定成主人私聊")
        prompt, why = prepare_prompt(text, max_chars=self.max_chars)
        if not prompt:
            return ImagePlan(False, why)
        if self.remaining_today() <= 0:
            return ImagePlan(False, f"今天已经画了 {self.used_today()} 张（上限 {self.daily_limit} 张），明天再说")
        try:
            grant = self.policy.grant_once(CAP_IMAGE, reason="主人这一句明确要图", scene=ctx.scene)
        except CapabilityDenied as exc:
            return ImagePlan(False, f"这次没有出图授权：{exc}")
        delivery = Delivery(
            id=f"img:{_digest(ctx.session_id + '|' + prompt)}",
            session_id=ctx.session_id,
            kind=KIND_TEXT,
            text=prompt,
            status=STATUS_PENDING,
            idem_key=f"image:{_digest(prompt)}",
            created_at=(now or self.clock.now()).isoformat(timespec="seconds"),
        )
        try:
            ensure_deliverable(delivery, grant)
        except CapabilityDenied as exc:
            return ImagePlan(False, str(exc))
        return ImagePlan(True, "", prompt, delivery, grant)

    # ---- 执行 ---------------------------------------------------------
    async def render(self, port: Any, ctx: InteractionContext, text: str) -> ImageOutcome:
        plan = self.plan(ctx, text)
        if not plan.ok:
            return ImageOutcome(False, plan.reason, fallback_text=self._fallback(ctx, text))
        request = ImageRequest(prompt=plan.prompt, scene=ctx.scene, max_chars=self.max_chars)
        try:
            result = await port.generate(request)
        except (ImageUnavailable, ImageContractError) as exc:
            return ImageOutcome(False, f"出图这条路走不通：{exc}", fallback_text=self._fallback(ctx, text))
        except Exception as exc:  # noqa: BLE001 - 端口炸了也只能退回文字
            return ImageOutcome(False, f"出图失败（{type(exc).__name__}）", fallback_text=self._fallback(ctx, text))
        if not isinstance(result, ImageResult):
            return ImageOutcome(False, "出图端口没有返回结果", fallback_text=self._fallback(ctx, text))
        if not result.ok:
            return ImageOutcome(
                False,
                result.reason or "出图没成",
                elapsed_ms=result.elapsed_ms,
                fallback_text=self._fallback(ctx, text),
            )
        self._roll()
        self._used += 1
        delivery = plan.delivery
        if delivery is not None:
            delivery = replace(delivery, extra={**dict(delivery.extra), "image_path": result.path})
        return ImageOutcome(
            True,
            "",
            path=result.path,
            elapsed_ms=result.elapsed_ms,
            extra={"prompt_id": result.prompt_id, "delivery": delivery.to_dict() if delivery else {}},
        )

    @staticmethod
    def _fallback(ctx: InteractionContext | None, text: str) -> str:
        who = "主人" if getattr(ctx, "is_owner", False) else "你"
        snippet = str(text or "").strip()
        if len(snippet) > 40:
            snippet = snippet[:40] + "…"
        return f"这张图没画成，先跟你说一声（{snippet or '没有描述'}）。{who}要是想，我可以只用文字讲讲。"


__all__ = ["ImageTurn", "ImagePlan", "ImageOutcome", "DEFAULT_DAILY_LIMIT"]
