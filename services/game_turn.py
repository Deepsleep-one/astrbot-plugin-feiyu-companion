"""M9-F：文字游戏的回合（开局 / 出招 / 认输）。

和前面几轮的回合一样守住同一套闸门：私聊、主人、绑定、一次性授权、日上限；
不同点是**这次是跟她玩**——所以她不能作弊、不能改规则、也不能事后解释：
每一回合都把「你出了什么、她出了什么、各自剩多少血」原样摊在回复里。

一局只在一个私聊会话里，最多一局；存档在 `services.game_book`，
重启之后还能接着打（`turn` 与双方血量都从库里读回来）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    from ..domain.context import InteractionContext
    from ..domain.delivery import KIND_TEXT, Delivery
    from ..domain.grants import CAP_GAME, PermissionPolicy
    from ..ports.game import (
        STATUS_PLAYING,
        GameError,
        GameState,
        abandon,
        apply,
        describe_result,
        describe_state,
        new_game_id,
        parse_move,
        resolve,
        verdict_line,
    )
except ImportError:  # 顶层兜底
    from astrbot_plugin_feiyu_companion.domain.context import InteractionContext
    from astrbot_plugin_feiyu_companion.domain.delivery import KIND_TEXT, Delivery
    from astrbot_plugin_feiyu_companion.domain.grants import CAP_GAME, PermissionPolicy
    from astrbot_plugin_feiyu_companion.ports.game import (
        STATUS_PLAYING,
        GameError,
        GameState,
        abandon,
        apply,
        describe_result,
        describe_state,
        new_game_id,
        parse_move,
        resolve,
        verdict_line,
    )

DEFAULT_DAILY_LIMIT = 10
DEFAULT_MAX_TURNS = 12
TZ = timezone(timedelta(hours=8))


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def game_enabled_from_config(cfg) -> bool:
    return _truthy(cfg.get("game_enable", False)) if cfg else False


def game_daily_limit_from_config(cfg) -> int:
    try:
        value = int((cfg or {}).get("game_daily_limit", DEFAULT_DAILY_LIMIT))
    except (TypeError, ValueError):
        value = DEFAULT_DAILY_LIMIT
    return max(1, min(50, value))


def game_max_turns_from_config(cfg) -> int:
    try:
        value = int((cfg or {}).get("game_max_turns", DEFAULT_MAX_TURNS))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_TURNS
    return max(4, min(40, value))


@dataclass(frozen=True)
class GamePlan:
    ok: bool
    reason: str = ""
    grant: object | None = None
    delivery: Delivery | None = None
    moment: datetime | None = None
    move: str = ""
    state: GameState | None = None


@dataclass(frozen=True)
class GameOutcome:
    ok: bool
    reason: str = ""
    text: str = ""
    state: GameState | None = None
    delivery: Delivery | None = None

    @property
    def needs_fallback(self) -> bool:
        return not self.ok

    @property
    def finished(self) -> bool:
        return bool(self.state and self.state.finished)


class GameTurn:
    """把「开局 / 出招 / 认输」三件事做成可测的一次性回合。"""

    def __init__(
        self,
        policy: PermissionPolicy,
        book,
        *,
        enabled: bool = False,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        max_turns: int = DEFAULT_MAX_TURNS,
        clock=None,
    ):
        self.policy = policy
        self.book = book
        self.enabled = bool(enabled)
        self.daily_limit = max(1, int(daily_limit))
        self.max_turns = max(4, int(max_turns))
        self.clock = clock
        self.last_error = ""

    # ---- 基础 -------------------------------------------------------
    def _now(self) -> datetime:
        if self.clock is not None and hasattr(self.clock, "now"):
            return self.clock.now()
        return datetime.now(TZ)

    def _digest(self, ctx: InteractionContext, action: str) -> str:
        raw = f"{action}|{ctx.session_id}|{self._now().isoformat(timespec='seconds')}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _delivery(self, ctx: InteractionContext, action: str) -> Delivery:
        digest = self._digest(ctx, action)
        return Delivery(
            id=f"game:{digest}",
            session_id=ctx.session_id,
            kind=KIND_TEXT,
            idem_key=f"game:{action}:{digest}",
        )

    def _gate(self, ctx: InteractionContext) -> str:
        """私聊 / 主人 / 绑定 三件事，缺一条就说清哪条。"""
        if not ctx.is_private:
            return "游戏只在私聊里玩：群里的每句话都有人在看"
        if not ctx.is_owner:
            return "这一桌只给主人"
        if not ctx.is_bound:
            return "还没认人：先在私聊里绑定，我才知道对面是你"
        return ""

    def used_today(self, owner_id: str, *, now: datetime | None = None) -> int:
        moment = now or self._now()
        return int(self.book.started_today(owner_id, now=moment))

    def remaining_today(self, owner_id: str, *, now: datetime | None = None) -> int:
        return max(0, self.daily_limit - self.used_today(owner_id, now=now))

    # ---- 开局 -------------------------------------------------------
    def plan_start(self, ctx: InteractionContext, *, now: datetime | None = None) -> GamePlan:
        moment = now or self._now()
        if not self.enabled:
            return GamePlan(False, "游戏关着（game_enable=false）：想玩就把开关打开")
        why = self._gate(ctx)
        if why:
            return GamePlan(False, why)
        active = self.book.active(ctx.user_id, ctx.session_id)
        if active is not None:
            return GamePlan(
                False,
                f"这一局还没结束（第 {active.turn + 1} 回合）：先出招，或者 /陪伴认输",
                state=active,
            )
        if self.remaining_today(ctx.user_id, now=moment) <= 0:
            return GamePlan(
                False,
                f"今天已经开了 {self.daily_limit} 局（上限 {self.daily_limit} 局），明天再玩",
            )
        try:
            grant = self.policy.grant_once(
                CAP_GAME, scene=ctx.scene, reason="主人这一句要玩一局"
            )
        except Exception as exc:  # noqa: BLE001
            return GamePlan(False, f"没拿到授权：{type(exc).__name__}")
        return GamePlan(
            True,
            grant=grant,
            delivery=self._delivery(ctx, "start"),
            moment=moment,
        )

    def start(self, ctx: InteractionContext, *, now: datetime | None = None) -> GameOutcome:
        plan = self.plan_start(ctx, now=now)
        if not plan.ok:
            return GameOutcome(False, plan.reason, state=plan.state)
        moment = plan.moment or self._now()
        seed = hashlib.sha1(
            f"{ctx.session_id}|{moment.isoformat(timespec='seconds')}".encode("utf-8")
        ).hexdigest()[:12]
        state = GameState(
            game_id=new_game_id(
                ctx.user_id,
                ctx.session_id,
                seed,
                ordinal=self.book.count_for(ctx.user_id, ctx.session_id),
            ),
            owner_id=ctx.user_id,
            session_id=ctx.session_id,
            seed=seed,
            max_turns=self.max_turns,
        )
        try:
            saved = self.book.create(state)
        except GameError as exc:
            return GameOutcome(False, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            return GameOutcome(False, f"开不了局（{type(exc).__name__}）：存档写不进去")
        text = (
            "开局。你和她各 10 点血，每人一招：攻 / 守 / 蓄。\n"
            f"{describe_state(saved)}\n"
            "（她也会出招；谁赢按规则算，不看我心情。）"
        )
        return GameOutcome(True, text=text, state=saved, delivery=plan.delivery)

    # ---- 出招 -------------------------------------------------------
    def plan_move(
        self, ctx: InteractionContext, raw: str, *, now: datetime | None = None
    ) -> GamePlan:
        moment = now or self._now()
        if not self.enabled:
            return GamePlan(False, "游戏关着（game_enable=false）：想玩就把开关打开")
        why = self._gate(ctx)
        if why:
            return GamePlan(False, why)
        active = self.book.active(ctx.user_id, ctx.session_id)
        if active is None:
            return GamePlan(False, "现在没有在打的一局：先 /陪伴玩 开一局")
        try:
            move = parse_move(raw)
        except GameError as exc:
            return GamePlan(False, str(exc), state=active)
        try:
            grant = self.policy.grant_once(
                CAP_GAME, scene=ctx.scene, reason="主人这一句出招"
            )
        except Exception as exc:  # noqa: BLE001
            return GamePlan(False, f"没拿到授权：{type(exc).__name__}")
        return GamePlan(
            True,
            grant=grant,
            delivery=self._delivery(ctx, "move"),
            moment=moment,
            move=move,
            state=active,
        )

    def move(self, ctx: InteractionContext, raw: str, *, now: datetime | None = None) -> GameOutcome:
        plan = self.plan_move(ctx, raw, now=now)
        if not plan.ok:
            return GameOutcome(False, plan.reason, state=plan.state)
        state = plan.state
        if state is None:  # pragma: no cover - 计划阶段已经拦住
            return GameOutcome(False, "没有在打的一局")
        try:
            result = resolve(state, plan.move)
        except GameError as exc:
            return GameOutcome(False, str(exc), state=state)
        after = apply(state, result, now_iso=(plan.moment or self._now()).isoformat())
        try:
            saved = self.book.save(after)
            self.book.record(result, game_id=after.game_id)
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            return GameOutcome(False, f"这回合没存下来（{type(exc).__name__}）：局面还是上一回合")
        lines = [describe_result(result)]
        if saved.finished:
            lines.append(verdict_line(saved))
            lines.append("（想再来一局：/陪伴玩）")
        else:
            lines.append(describe_state(saved))
            lines.append("（下一招：/陪伴出招 攻|守|蓄）")
        return GameOutcome(True, text="\n".join(lines), state=saved, delivery=plan.delivery)

    # ---- 认输 -------------------------------------------------------
    def plan_abandon(self, ctx: InteractionContext, *, now: datetime | None = None) -> GamePlan:
        moment = now or self._now()
        if not self.enabled:
            return GamePlan(False, "游戏关着（game_enable=false）")
        why = self._gate(ctx)
        if why:
            return GamePlan(False, why)
        active = self.book.active(ctx.user_id, ctx.session_id)
        if active is None:
            return GamePlan(False, "现在没有在打的一局")
        return GamePlan(
            True,
            delivery=self._delivery(ctx, "abandon"),
            moment=moment,
            state=active,
        )

    def give_up(self, ctx: InteractionContext, *, now: datetime | None = None) -> GameOutcome:
        plan = self.plan_abandon(ctx, now=now)
        if not plan.ok:
            return GameOutcome(False, plan.reason, state=plan.state)
        state = plan.state
        if state is None:  # pragma: no cover
            return GameOutcome(False, "现在没有在打的一局")
        try:
            after = abandon(state, now_iso=(plan.moment or self._now()).isoformat())
            saved = self.book.save(after)
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            return GameOutcome(False, f"认输没记下来（{type(exc).__name__}）")
        return GameOutcome(True, text=verdict_line(saved), state=saved, delivery=plan.delivery)

    # ---- 状态 -------------------------------------------------------
    def current(self, ctx: InteractionContext) -> GameState | None:
        return self.book.active(ctx.user_id, ctx.session_id)

    def status_line(self, owner_id: str = "", *, now: datetime | None = None) -> str:
        if not self.enabled:
            return "关着（game_enable=false，一局都不开）"
        if not owner_id:
            return f"就绪；每天最多 {self.daily_limit} 局"
        return f"就绪；今天已开 {self.used_today(owner_id, now=now)}/{self.daily_limit} 局"


__all__ = [
    "DEFAULT_DAILY_LIMIT",
    "DEFAULT_MAX_TURNS",
    "GameOutcome",
    "GamePlan",
    "GameTurn",
    "game_daily_limit_from_config",
    "game_enabled_from_config",
    "game_max_turns_from_config",
]
