"""M8-B/M8-D：纪念日（公历 + 农历）。

- 公历：按 `(month, day)` 每年一次；2 月 29 日只在闰年长出来，平年不挪到 3 月 1 日
  （那是在替你改生日）。
- 农历：走 `domain/lunar.py` 的历表（1900–2100）。闰月可以显式指定（`leap=True`）；
  闰月那条只在有那个闰月的年份才成立，其它年份 `occurs_on()` 返回 None，不猜。
- 时区显式写进每一条（默认 Asia/Shanghai），"今天"按它算，不按服务器本地时间。
- 「一年只提醒一次」由 `AnniversaryBook` 记账，这里只回答「下一次是哪天、还有几天」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import lunar

CAL_SOLAR = "solar"
CAL_LUNAR = "lunar"
CALENDARS = (CAL_SOLAR, CAL_LUNAR)
KINDS = ("birthday", "memorial", "anniversary", "other")
DEFAULT_TZ = "Asia/Shanghai"
MAX_TITLE = 40
MAX_REMIND_DAYS = 30
LUNAR_YEARS_AHEAD = 12  # 闰月那条可能好几年才轮到一次


class AnniversaryError(ValueError):
    """这条纪念日写得不对。"""


class LunarUnsupported(AnniversaryError):
    """农历日子算不出来——超出历表范围，或那一年没有这个闰月。"""


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AnniversaryError(f"不认识的时区：{name}") from exc


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


@dataclass(frozen=True)
class Anniversary:
    id: str
    title: str
    month: int
    day: int
    calendar: str = CAL_SOLAR
    leap: bool = False
    tz: str = DEFAULT_TZ
    remind_days: int = 0
    kind: str = "other"
    note: str = ""
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        title = (self.title or "").strip()
        if not title:
            raise AnniversaryError("纪念日总得有个名字")
        if len(title) > MAX_TITLE:
            raise AnniversaryError(f"名字太长（最多 {MAX_TITLE} 字）")
        if self.calendar not in CALENDARS:
            raise AnniversaryError(f"不认识的历法：{self.calendar}")
        if not isinstance(self.month, int) or not 1 <= self.month <= 12:
            raise AnniversaryError(f"月份要在 1–12 之间：{self.month}")
        top = 30 if self.calendar == CAL_LUNAR else 31
        if not isinstance(self.day, int) or not 1 <= self.day <= top:
            raise AnniversaryError(f"日期要在 1–{top} 之间：{self.day}")
        if self.leap and self.calendar != CAL_LUNAR:
            raise AnniversaryError("只有农历才有闰月")
        if self.kind not in KINDS:
            raise AnniversaryError(f"不认识的类型：{self.kind}")
        remind_days = int(self.remind_days or 0)
        if not 0 <= remind_days <= MAX_REMIND_DAYS:
            raise AnniversaryError(f"提前提醒要在 0–{MAX_REMIND_DAYS} 天之间")
        _zone(self.tz)
        if self.calendar == CAL_LUNAR:
            self._check_lunar()

    def _check_lunar(self) -> None:
        """确认这个农历月日真的存在于某个年份里，否则当场说清。"""
        found = False
        for year in range(2020, 2020 + LUNAR_YEARS_AHEAD):
            try:
                lunar.lunar_to_solar(year, self.month, self.day, self.leap)
            except lunar.LunarError:
                continue
            found = True
            break
        if not found:
            name = lunar.month_name(self.month, self.leap)
            raise LunarUnsupported(
                f"农历{name}{lunar.day_name(self.day)} 在近几十年里都算不出来："
                "日子或闰月写错了？"
            )

    # ---- 计算 -------------------------------------------------------
    def occurs_on(self, year: int) -> date | None:
        """这一年它落在哪一天；不成立就 None（不替你挪日子）。"""
        if self.calendar == CAL_LUNAR:
            try:
                return lunar.lunar_to_solar(year, self.month, self.day, self.leap)
            except lunar.LunarError:
                return None
        if self.month == 2 and self.day == 29:
            leap = (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
            if not leap:
                return None
        if self.day > _days_in_month(year, self.month):
            return None
        return date(year, self.month, self.day)

    def _local(self, now: datetime) -> datetime:
        zone = _zone(self.tz)
        if now.tzinfo is None:
            return now.replace(tzinfo=zone)
        return now.astimezone(zone)

    def next_occurrence(self, now: datetime) -> date | None:
        """从 now 起（含今天）最近的一次。农历要往前多看几年（闰月）。"""
        local = self._local(now)
        today = local.date()
        ahead = LUNAR_YEARS_AHEAD if self.calendar == CAL_LUNAR else 4
        start = today.year - 1 if self.calendar == CAL_LUNAR else today.year
        for year in range(start, today.year + ahead):
            moment = self.occurs_on(year)
            if moment is None:
                continue
            if moment >= today:
                return moment
        return None

    def days_until(self, now: datetime) -> int | None:
        moment = self.next_occurrence(now)
        if moment is None:
            return None
        return (moment - self._local(now).date()).days

    def is_today(self, now: datetime) -> bool:
        local = self._local(now)
        return self.next_occurrence(now) == local.date()

    def is_due(self, now: datetime) -> bool:
        """今天正好，或落在提前提醒的窗口里。"""
        left = self.days_until(now)
        return left is not None and left <= int(self.remind_days or 0)

    @property
    def date_label(self) -> str:
        """写给人看的日子：「03-14」或「农历闰六月十五」这种。"""
        if self.calendar == CAL_LUNAR:
            return "农历" + lunar.month_name(self.month, self.leap) + lunar.day_name(self.day)
        return f"{self.month:02d}-{self.day:02d}"

    # ---- 序列化 -----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "month": int(self.month),
            "day": int(self.day),
            "calendar": self.calendar,
            "leap": bool(self.leap),
            "tz": self.tz,
            "remind_days": int(self.remind_days),
            "kind": self.kind,
            "note": self.note,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Anniversary":
        if not isinstance(raw, dict):
            raise AnniversaryError("纪念日要是一个对象")
        missing = [k for k in ("title", "month", "day") if raw.get(k) in (None, "")]
        if missing:
            raise AnniversaryError(f"缺字段：{', '.join(missing)}")
        try:
            month = int(raw.get("month"))
            day = int(raw.get("day"))
        except (TypeError, ValueError) as exc:
            raise AnniversaryError("月份和日期要是数字") from exc
        return cls(
            id=str(raw.get("id") or ""),
            title=str(raw.get("title")),
            month=month,
            day=day,
            calendar=str(raw.get("calendar") or CAL_SOLAR),
            leap=bool(raw.get("leap") or False),
            tz=str(raw.get("tz") or DEFAULT_TZ),
            remind_days=int(raw.get("remind_days") or 0),
            kind=str(raw.get("kind") or "other"),
            note=str(raw.get("note") or ""),
            extra=dict(raw.get("extra") or {}),
        )

    def light(self, now: datetime) -> dict:
        """给状态页/提示词用的一行摘要。"""
        left = self.days_until(now)
        moment = self.next_occurrence(now)
        return {
            "title": self.title,
            "kind": self.kind,
            "date": self.date_label,
            "calendar": self.calendar,
            "next": moment.isoformat() if moment else "",
            "days_until": left,
            "tz": self.tz,
        }


def human_left(days: int | None) -> str:
    if days is None:
        return "算不出下一次"
    if days == 0:
        return "就是今天"
    if days == 1:
        return "明天"
    return f"还有 {days} 天"


__all__ = [
    "CAL_SOLAR",
    "CAL_LUNAR",
    "CALENDARS",
    "KINDS",
    "DEFAULT_TZ",
    "MAX_TITLE",
    "MAX_REMIND_DAYS",
    "LUNAR_YEARS_AHEAD",
    "Anniversary",
    "AnniversaryError",
    "LunarUnsupported",
    "human_left",
]
