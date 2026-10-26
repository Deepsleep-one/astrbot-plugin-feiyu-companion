"""时钟抽象。

预算、退避、日程都要「现在几点」，但测试不能等真实时间，所以统一走这里。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

TZ = timezone(timedelta(hours=8))


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(TZ)

    def monotonic(self) -> float:
        return datetime.now(TZ).timestamp()


class FakeClock:
    """假时钟：测试里 `advance(seconds)` 推时间，`monotonic` 跟着走。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 25, 9, 0, 0, tzinfo=TZ)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._now.timestamp()

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, moment: datetime) -> datetime:
        self._now = moment
        return self._now
