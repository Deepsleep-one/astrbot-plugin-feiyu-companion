"""预算与退避（M3，纯内存 + 假时钟可测）。

三条限制先按保守值实现，接线到真实运行时是 M5 的事：
- 每日主动消息条数上限（默认 2 条，与 B站主动分享的 2 条/日对齐）；
- 每分钟动作上限（防止一时冲动连发）；
- 失败退避：指数增长并封顶，避免对着坏掉的接口猛敲。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.grants import CAP_PROACTIVE
from ..runtime.clock import Clock, SystemClock


@dataclass
class BudgetPolicy:
    daily_proactive: int = 2
    per_minute_actions: int = 6
    backoff_base: float = 30.0
    backoff_max: float = 1800.0

    def to_dict(self) -> dict:
        return {
            "daily_proactive": self.daily_proactive,
            "per_minute_actions": self.per_minute_actions,
            "backoff_base": self.backoff_base,
            "backoff_max": self.backoff_max,
        }


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str = ""
    retry_at: str = ""
    remaining_today: int = 0

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "retry_at": self.retry_at,
            "remaining_today": self.remaining_today,
        }


class Budget:
    """记账本。所有判断都以 `clock` 为准，测试用假时钟推进。"""

    WINDOW_SECONDS = 60.0

    def __init__(self, policy: BudgetPolicy | None = None, clock: Clock | None = None) -> None:
        self.policy = policy or BudgetPolicy()
        self.clock = clock or SystemClock()
        self._day = ""
        self._proactive_today = 0
        self._shared_today = 0
        self._events: list[float] = []
        self._failures = 0
        self._blocked_until = 0.0

    # ---- 内部 -------------------------------------------------------------
    def _roll_day(self) -> None:
        today = self.clock.now().date().isoformat()
        if today != self._day:
            self._day = today
            self._proactive_today = 0
            self._shared_today = 0

    def _trim(self, mono: float) -> None:
        cutoff = mono - self.WINDOW_SECONDS
        self._events = [ts for ts in self._events if ts > cutoff]

    def _remaining(self) -> int:
        return max(0, self.policy.daily_proactive - self._proactive_today)

    def _retry_iso(self, wait_seconds: float) -> str:
        return (self.clock.now() + _seconds(wait_seconds)).isoformat(timespec="seconds")

    # ---- 对外 -------------------------------------------------------------
    def charge_share(self) -> int:
        """一条分享也算一条主动消息（合账），返回记账后今天还剩几条。

        「这条该不该发」仍由 B站 那边判（额度 / 冷却 / 免打扰），这里只保证
        主动聊天和分享共用同一个日上限，不出现两条账各数各的。
        """
        self._roll_day()
        self._proactive_today += 1
        self._shared_today += 1
        return self._remaining()

    def check(self, action: str = CAP_PROACTIVE) -> BudgetDecision:
        self._roll_day()
        mono = self.clock.monotonic()
        if mono < self._blocked_until:
            return BudgetDecision(
                allowed=False,
                reason="失败退避中",
                retry_at=self._retry_iso(self._blocked_until - mono),
                remaining_today=self._remaining(),
            )
        self._trim(mono)
        if len(self._events) >= self.policy.per_minute_actions:
            oldest = min(self._events)
            return BudgetDecision(
                allowed=False,
                reason="每分钟动作已达上限",
                retry_at=self._retry_iso(oldest + self.WINDOW_SECONDS - mono),
                remaining_today=self._remaining(),
            )
        if action == CAP_PROACTIVE and self._proactive_today >= self.policy.daily_proactive:
            return BudgetDecision(
                allowed=False,
                reason="今天主动消息已经发满",
                remaining_today=0,
            )
        return BudgetDecision(allowed=True, reason="", remaining_today=self._remaining())

    def consume(self, action: str = CAP_PROACTIVE) -> BudgetDecision:
        decision = self.check(action)
        if not decision.allowed:
            return decision
        mono = self.clock.monotonic()
        self._events.append(mono)
        self._trim(mono)
        if action == CAP_PROACTIVE:
            self._proactive_today += 1
        return BudgetDecision(
            allowed=True,
            reason="已记账",
            remaining_today=self._remaining(),
        )

    def record_failure(self) -> float:
        """记一次失败，返回这次要退避多少秒（调用方据此排下次重试）。"""
        self._failures += 1
        wait = self.policy.backoff_base * (2 ** max(0, self._failures - 1))
        wait = min(wait, self.policy.backoff_max)
        self._blocked_until = self.clock.monotonic() + wait
        return wait

    def record_success(self) -> None:
        self._failures = 0
        self._blocked_until = 0.0

    def snapshot(self) -> dict:
        self._roll_day()
        return {
            "day": self._day,
            "proactive_today": self._proactive_today,
            "shared_today": self._shared_today,
            "remaining_today": self._remaining(),
            "failures": self._failures,
            "blocked": self.clock.monotonic() < self._blocked_until,
            "policy": self.policy.to_dict(),
        }


def _seconds(value: float):
    from datetime import timedelta

    return timedelta(seconds=value)
