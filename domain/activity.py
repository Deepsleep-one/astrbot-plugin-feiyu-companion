"""生活活动契约（M4）。

关键一条：`simulated=True` 的活动**只能变成叙述**，永远不能触发真实动作。
「她今天下午去楼下走了走」是模拟出来的生活，不是她真的出了门——
所以这里连执行入口都不提供，只提供 `narrate()`。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

ACTIVITY_KINDS = ("routine", "work", "meal", "outdoor", "study", "rest", "social")

STATUS_PLANNED = "planned"
STATUS_ACTIVE = "active"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"

ACTIVITY_STATUSES = (STATUS_PLANNED, STATUS_ACTIVE, STATUS_DONE, STATUS_SKIPPED)


class ActivityError(ValueError):
    """活动本身不成立（时间反了、缺时区之类）。"""


class SimulatedActivityError(PermissionError):
    """想把模拟出来的生活当真实动作执行。"""


@dataclass(frozen=True)
class Activity:
    id: str
    kind: str
    title: str
    start: datetime
    end: datetime
    place: str = ""
    with_whom: tuple[str, ...] = ()
    simulated: bool = True
    status: str = STATUS_PLANNED
    note: str = ""
    say: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ACTIVITY_KINDS:
            raise ActivityError(f"未知活动类型：{self.kind}")
        if self.status not in ACTIVITY_STATUSES:
            raise ActivityError(f"未知活动状态：{self.status}")
        for label, moment in (("start", self.start), ("end", self.end)):
            if moment.tzinfo is None:
                raise ActivityError(f"活动 {self.id} 的 {label} 缺时区")
        if self.end <= self.start:
            raise ActivityError(f"活动 {self.id} 结束时间不在开始之后")

    @property
    def span_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def is_real(self) -> bool:
        return not self.simulated

    def covers(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def overlaps(self, other: "Activity") -> bool:
        return self.start < other.end and other.start < self.end

    def assert_executable(self) -> None:
        """任何「真去做」的路径都必须先过这一关。"""
        if self.simulated:
            raise SimulatedActivityError(
                f"活动 {self.id}（{self.title}）是模拟生活，不能当真实动作执行"
            )

    def narrate(self) -> str:
        """叙述用的一句话。模拟生活只能从这里出去。"""
        if self.say:
            return self.say
        place = f"在{self.place}" if self.place else ""
        return f"{place}{self.title}".strip()

    def with_status(self, status: str) -> "Activity":
        return replace(self, status=status)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "start": self.start.isoformat(timespec="minutes"),
            "end": self.end.isoformat(timespec="minutes"),
            "place": self.place,
            "with_whom": list(self.with_whom),
            "simulated": self.simulated,
            "status": self.status,
            "note": self.note,
            "say": self.say,
        }
