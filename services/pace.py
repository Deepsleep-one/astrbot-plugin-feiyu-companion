"""随机节奏：下次开口的点，由骰子决定。

一轮检查只回答一个问题：「现在该不该说话」。以前是「静默满 N 分钟」，
现在改成「排的点到了没有」——每次说完一句、或主人刚说过话，就掷一次骰子，
把下一次的时间点写进状态文件。

骰子可以只有一个区间（`Pace(5, 180)`），也可以分时段（白天短、深夜长）：

    Pace(bands=(
        Band("07:00", "18:00", 5, 45, "白天"),
        Band("18:00", "22:30", 10, 90, "傍晚"),
        Band("22:30", "07:00", 30, 180, "深夜"),
    ))

几条规矩（测试会逐条检查）：

- 上下界写反了自动交换，不报错；
- 负数归零，非数字 / NaN / inf 回落默认；
- 两边一样就是固定节奏（`fixed` 为真）；
- 跨午夜的时段（如 22:30–07:00）靠 `t >= start or t < end` 判断。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone

DEFAULT_MIN_MINUTES = 5.0
DEFAULT_MAX_MINUTES = 180.0

TZ = timezone(timedelta(hours=8))

_DEFAULT_PAIRS = (DEFAULT_MIN_MINUTES, DEFAULT_MAX_MINUTES)


def _clean(value, fallback: float) -> float:
    """把配置里来的东西变成能用的分钟数。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if math.isnan(number) or math.isinf(number):
        return float(fallback)
    return max(0.0, number)


def _pair(lo, hi, fallback: tuple[float, float] = _DEFAULT_PAIRS) -> tuple[float, float]:
    low = _clean(lo, fallback[0])
    high = _clean(hi, fallback[1])
    if high < low:
        low, high = high, low
    return low, high


def parse_hhmm(text: str, default: str = "00:00") -> dtime:
    """把 "09:00" / "9:00" 解析成时间；解析不了就用默认。"""
    raw = str(text or "").strip().replace("：", ":")
    for candidate in (raw, default):
        if not candidate:
            continue
        parts = candidate.split(":")
        if len(parts) != 2:
            continue
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return dtime(hour, minute)
    return dtime(0, 0)


@dataclass(frozen=True)
class Band:
    """一个时段：在 start–end 之间掷 min–max 分钟的骰子。"""

    start: str
    end: str
    min_minutes: float
    max_minutes: float
    name: str = ""

    def __post_init__(self) -> None:
        low, high = _pair(self.min_minutes, self.max_minutes)
        object.__setattr__(self, "min_minutes", low)
        object.__setattr__(self, "max_minutes", high)

    @property
    def fixed(self) -> bool:
        return abs(self.max_minutes - self.min_minutes) < 1e-9

    @property
    def start_at(self) -> dtime:
        return parse_hhmm(self.start, "00:00")

    @property
    def end_at(self) -> dtime:
        return parse_hhmm(self.end, "00:00")

    def covers(self, moment: datetime) -> bool:
        start, end, now = self.start_at, self.end_at, moment.timetz().replace(tzinfo=None)
        if start == end:
            return True
        if start < end:
            return start <= now < end
        # 跨午夜：22:30–07:00
        return now >= start or now < end

    def gap_minutes(self, rng: random.Random | None = None) -> float:
        if self.fixed:
            return self.min_minutes
        return float((rng or random).uniform(self.min_minutes, self.max_minutes))

    def describe(self) -> str:
        label = f"{self.name} " if self.name else ""
        if self.fixed:
            return f"{label}每 {self.min_minutes:g} 分钟（{self.start}–{self.end}）"
        return (f"{label}{self.min_minutes:g}–{self.max_minutes:g} 分钟"
                f"（{self.start}–{self.end}）")


@dataclass(frozen=True)
class Pace:
    """骰子的全部规矩。`bands` 为空时就是单一区间。"""

    min_minutes: float = DEFAULT_MIN_MINUTES
    max_minutes: float = DEFAULT_MAX_MINUTES
    bands: tuple[Band, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        low, high = _pair(self.min_minutes, self.max_minutes)
        object.__setattr__(self, "min_minutes", low)
        object.__setattr__(self, "max_minutes", high)
        cleaned = tuple(b for b in (self.bands or ()) if isinstance(b, Band))
        object.__setattr__(self, "bands", cleaned)

    @property
    def fixed(self) -> bool:
        return not self.bands and abs(self.max_minutes - self.min_minutes) < 1e-9

    def band_at(self, moment: datetime | None = None) -> Band | None:
        if not self.bands:
            return None
        when = moment or datetime.now(TZ)
        for band in self.bands:
            if band.covers(when):
                return band
        return self.bands[0]

    def gap_minutes(self, rng: random.Random | None = None,
                    at: datetime | None = None) -> float:
        band = self.band_at(at)
        if band is not None:
            return band.gap_minutes(rng)
        if self.fixed:
            return self.min_minutes
        return float((rng or random).uniform(self.min_minutes, self.max_minutes))

    def gap_seconds(self, rng: random.Random | None = None,
                    at: datetime | None = None) -> float:
        return self.gap_minutes(rng, at) * 60.0

    def next_due(self, anchor: datetime, rng: random.Random | None = None) -> datetime:
        return anchor + timedelta(minutes=self.gap_minutes(rng, at=anchor))

    def describe(self) -> str:
        if self.bands:
            return " / ".join(band.describe() for band in self.bands)
        if self.fixed:
            return f"每 {self.min_minutes:g} 分钟"
        return f"{self.min_minutes:g}–{self.max_minutes:g} 分钟之间随机"

    def to_dict(self) -> dict:
        return {
            "min_minutes": self.min_minutes,
            "max_minutes": self.max_minutes,
            "fixed": self.fixed,
            "bands": [
                {
                    "name": band.name,
                    "start": band.start,
                    "end": band.end,
                    "min_minutes": band.min_minutes,
                    "max_minutes": band.max_minutes,
                }
                for band in self.bands
            ],
        }


# 默认分时段：白天短、傍晚中等、深夜长
DEFAULT_BANDS = (
    Band("07:00", "18:00", 5.0, 45.0, "白天"),
    Band("18:00", "22:30", 10.0, 90.0, "傍晚"),
    Band("22:30", "07:00", 30.0, 180.0, "深夜"),
)

# 配置里每组时段的三/四个键
BAND_KEYS = (
    ("白天", "day", "07:00", "18:00", 5.0, 45.0),
    ("傍晚", "evening", "18:00", "22:30", 10.0, 90.0),
    ("深夜", "night", "22:30", "07:00", 30.0, 180.0),
)


def pace_from_config(config: dict | None) -> Pace:
    """从插件配置读骰子规矩。

    配了任一时段键就按时段走；一个都没配就退回单一区间
    （`interval_min_minutes` / `interval_max_minutes`）。
    """
    cfg = dict(config or {})

    def get(key, fallback=None):
        value = cfg.get(key, fallback)
        return fallback if value in (None, "") else value

    has_band = any(
        str(cfg.get(f"interval_{slug}_max_minutes") or "").strip()
        or str(cfg.get(f"interval_{slug}_min_minutes") or "").strip()
        for _, slug, *_ in BAND_KEYS
    )

    if has_band:
        bands = tuple(
            Band(
                str(get(f"interval_{slug}_start", start)),
                str(get(f"interval_{slug}_end", end)),
                get(f"interval_{slug}_min_minutes", low),
                get(f"interval_{slug}_max_minutes", high),
                name,
            )
            for name, slug, start, end, low, high in BAND_KEYS
        )
        low, high = _pair(get("interval_min_minutes", DEFAULT_MIN_MINUTES),
                          get("interval_max_minutes", DEFAULT_MAX_MINUTES))
        return Pace(low, high, bands)

    return Pace(
        get("interval_min_minutes", DEFAULT_MIN_MINUTES),
        get("interval_max_minutes", DEFAULT_MAX_MINUTES),
    )
