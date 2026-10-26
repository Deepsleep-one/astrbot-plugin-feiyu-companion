"""直播状态的一次性回合（M9-E）。

同一套闸门：私聊、主人、绑定、一次性授权、冷却、日上限。
不同点：这次看的不是主人自己的机器，而是**别人的公开直播间状态**——
所以更要克制：只看开播与否/在线人数/标题，不进房间、不取弹幕、不发任何东西。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

try:
    from ..domain.context import InteractionContext
    from ..domain.delivery import KIND_TEXT, Delivery
    from ..domain.grants import CAP_LIVE, CapabilityDenied, PermissionPolicy
    from ..ports.live import (
        LiveError,
        LiveQuery,
        LiveSnapshot,
        LiveUnavailable,
        describe,
        parse_room,
    )
except ImportError:  # 顶层兜底
    from astrbot_plugin_feiyu_companion.domain.context import InteractionContext
    from astrbot_plugin_feiyu_companion.domain.delivery import KIND_TEXT, Delivery
    from astrbot_plugin_feiyu_companion.domain.grants import (
        CAP_LIVE,
        CapabilityDenied,
        PermissionPolicy,
    )
    from astrbot_plugin_feiyu_companion.ports.live import (
        LiveError,
        LiveQuery,
        LiveSnapshot,
        LiveUnavailable,
        describe,
        parse_room,
    )

DEFAULT_DAILY_LIMIT = 5
DEFAULT_COOLDOWN_SECONDS = 60
TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class LivePlan:
    ok: bool
    reason: str = ""
    query: LiveQuery | None = None
    delivery: Delivery | None = None
    grant: object | None = None
    moment: datetime | None = None


@dataclass(frozen=True)
class LiveOutcome:
    ok: bool
    reason: str = ""
    text: str = ""
    delivery: Delivery | None = None
    room_id: int = 0
    captured_at: str = ""
    expires_at: str = ""
    notes: tuple[str, ...] = ()

    @property
    def needs_fallback(self) -> bool:
        return not self.ok


class LiveTurn:
    def __init__(
        self,
        policy: PermissionPolicy,
        *,
        max_chars: int = 1200,
        ttl_seconds: int = 60,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        clock: object | None = None,
    ) -> None:
        self.policy = policy
        self.max_chars = max(80, int(max_chars))
        self.ttl_seconds = max(1, min(300, int(ttl_seconds)))
        self.daily_limit = max(1, int(daily_limit))
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self._clock = clock
        self._used = 0
        self._last_at: datetime | None = None

    # ---- 时钟与计数 ----
    def _now(self, moment: datetime | None = None) -> datetime:
        if moment is not None:
            return moment if moment.tzinfo else moment.replace(tzinfo=TZ)
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(TZ)

    def _roll(self, moment: datetime) -> None:
        if self._last_at is None:
            return
        if self._last_at.astimezone(TZ).date() != moment.astimezone(TZ).date():
            self._used = 0

    def used_today(self, now: datetime | None = None) -> int:
        moment = self._now(now)
        self._roll(moment)
        return self._used

    def remaining_today(self, now: datetime | None = None) -> int:
        return max(0, self.daily_limit - self.used_today(now))

    # ---- 计划 ----
    def plan(
        self,
        ctx: InteractionContext,
        target: str | int,
        *,
        now: datetime | None = None,
    ) -> LivePlan:
        moment = self._now(now)
        self._roll(moment)
        if not ctx.is_private:
            return LivePlan(False, "直播间状态只在私聊里看：群里不替主人开别人的直播")
        if not ctx.is_owner:
            return LivePlan(False, "只有主人能让我看直播间的状态")
        if not ctx.is_bound:
            return LivePlan(False, "这个会话还没绑定主人：先绑定了再看")
        if self.cooldown_seconds and self._last_at is not None:
            elapsed = (moment - self._last_at).total_seconds()
            if elapsed < self.cooldown_seconds:
                left = int(self.cooldown_seconds - elapsed)
                return LivePlan(False, f"刚看过一次，再等 {left} 秒")
        if self._used >= self.daily_limit:
            return LivePlan(
                False, f"今天已经看了 {self._used} 次（上限 {self.daily_limit} 次），明天再说"
            )

        try:
            room_id = parse_room(target if isinstance(target, str) else str(int(target)))
            query = LiveQuery(room_id=room_id, scene=ctx.scene, ttl_seconds=self.ttl_seconds)
        except LiveError as exc:
            return LivePlan(False, str(exc))

        try:
            grant = self.policy.grant_once(
                CAP_LIVE,
                scene=ctx.scene,
                reason=f"这一句明确要看直播间 {query.room_id} 的状态",
                expires_at=(moment + timedelta(seconds=self.ttl_seconds)).isoformat(
                    timespec="seconds"
                ),
            )
        except CapabilityDenied as exc:
            return LivePlan(False, str(exc))

        digest = hashlib.sha1(
            f"{ctx.session_id}|{query.room_id}|{moment.isoformat(timespec='seconds')}".encode(
                "utf-8"
            )
        ).hexdigest()[:16]
        delivery = Delivery(
            id=f"live:{digest}",
            session_id=ctx.session_id,
            kind=KIND_TEXT,
            idem_key=f"live:{digest}",
            text=f"直播间 {query.room_id} 的状态",
        )
        return LivePlan(
            True, "", query=query, delivery=delivery, grant=grant, moment=moment
        )

    # ---- 执行 ----
    def render(self, port: object, plan: LivePlan) -> LiveOutcome:
        if not plan.ok or plan.query is None:
            return LiveOutcome(False, plan.reason or "这一条没成")
        try:
            snapshot = port.snapshot(plan.query)
        except LiveUnavailable as exc:
            return LiveOutcome(False, self._why(str(exc)))
        except Exception as exc:  # noqa: BLE001
            return LiveOutcome(False, f"没看成：看的时候出了 {type(exc).__name__}")
        return self._outcome(plan, snapshot)

    async def render_async(self, port: object, plan: LivePlan) -> LiveOutcome:
        if not plan.ok or plan.query is None:
            return LiveOutcome(False, plan.reason or "这一条没成")
        getter = getattr(port, "snapshot_async", None)
        try:
            snapshot = (
                await getter(plan.query) if getter is not None else port.snapshot(plan.query)
            )
        except LiveUnavailable as exc:
            return LiveOutcome(False, self._why(str(exc)))
        except Exception as exc:  # noqa: BLE001
            return LiveOutcome(False, f"没看成：看的时候出了 {type(exc).__name__}")
        return self._outcome(plan, snapshot)

    def _why(self, reason: str) -> str:
        text = str(reason or "").strip()
        if not text:
            return "没看成：接口没说原因"
        if "integration_pending" in text:
            tail = text.split("integration_pending", 1)[1].lstrip("：: ").strip()
            return f"看不了直播间（直播只读关着）：{tail}" if tail else "看不了直播间"
        return f"没看成：{text}"

    def _outcome(self, plan: LivePlan, snapshot: LiveSnapshot) -> LiveOutcome:
        if not isinstance(snapshot, LiveSnapshot):
            return LiveOutcome(False, "接口返回的东西不对")
        if not snapshot.ok:
            return LiveOutcome(False, f"没看成：{snapshot.notes[0] if snapshot.notes else '接口没给状态'}")

        self._used += 1
        self._last_at = plan.moment or self._now()
        text = describe(snapshot)
        if len(text) > self.max_chars:
            text = text[: self.max_chars - 1].rstrip() + "…"
        delivery = plan.delivery
        if delivery is not None:
            delivery = replace(delivery, text=text, extra={"room_id": snapshot.room_id})
        return LiveOutcome(
            True,
            "",
            text=text,
            delivery=delivery,
            room_id=snapshot.room_id,
            captured_at=snapshot.captured_at,
            expires_at=snapshot.expires_at,
            notes=tuple(snapshot.notes),
        )


__all__ = [
    "DEFAULT_DAILY_LIMIT",
    "DEFAULT_COOLDOWN_SECONDS",
    "LivePlan",
    "LiveOutcome",
    "LiveTurn",
]
