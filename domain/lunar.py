"""农历换算（1900–2100），只做「公历日期 ↔ 农历月日」这一件事。

做法是查表：每个农历年一个字（`LUNAR_INFO`），编码
- bit 16..5 十二/十三个月的月大小（1 = 30 天，0 = 29 天），
- bit 4..0 闰月月份（0 表示这一年没有闰月），
- 闰月天数由 bit 16 表示（1 = 30 天）。

基准：公历 1900-01-31 是农历 1900 年正月初一。
超出 1900–2100 直接抛 `LunarRangeError`——表里没有的年份不猜。

这张表与原版 `lunardate` / 各家 JS 实现同源；本文件末尾的 `selftest_lunar.py`
用几个公开的春节/中秋日期做锚点校验，改表必须重跑那套测试。
"""
from __future__ import annotations

from datetime import date, timedelta

MIN_YEAR = 1900
MAX_YEAR = 2100
BASE_SOLAR = date(1900, 1, 31)  # 农历 1900 年正月初一

LUNAR_INFO = [
    0x04BD8, 0x04AE0, 0x0A570, 0x054D5, 0x0D260, 0x0D950, 0x16554, 0x056A0, 0x09AD0, 0x055D2,  # 1900
    0x04AE0, 0x0A5B6, 0x0A4D0, 0x0D250, 0x1D255, 0x0B540, 0x0D6A0, 0x0ADA2, 0x095B0, 0x14977,  # 1910
    0x04970, 0x0A4B0, 0x0B4B5, 0x06A50, 0x06D40, 0x1AB54, 0x02B60, 0x09570, 0x052F2, 0x04970,  # 1920
    0x06566, 0x0D4A0, 0x0EA50, 0x06E95, 0x05AD0, 0x02B60, 0x186E3, 0x092E0, 0x1C8D7, 0x0C950,  # 1930
    0x0D4A0, 0x1D8A6, 0x0B550, 0x056A0, 0x1A5B4, 0x025D0, 0x092D0, 0x0D2B2, 0x0A950, 0x0B557,  # 1940
    0x06CA0, 0x0B550, 0x15355, 0x04DA0, 0x0A5B0, 0x14573, 0x052B0, 0x0A9A8, 0x0E950, 0x06AA0,  # 1950
    0x0AEA6, 0x0AB50, 0x04B60, 0x0AAE4, 0x0A570, 0x05260, 0x0F263, 0x0D950, 0x05B57, 0x056A0,  # 1960
    0x096D0, 0x04DD5, 0x04AD0, 0x0A4D0, 0x0D4D4, 0x0D250, 0x0D558, 0x0B540, 0x0B6A0, 0x195A6,  # 1970
    0x095B0, 0x049B0, 0x0A974, 0x0A4B0, 0x0B27A, 0x06A50, 0x06D40, 0x0AF46, 0x0AB60, 0x09570,  # 1980
    0x04AF5, 0x04970, 0x064B0, 0x074A3, 0x0EA50, 0x06B58, 0x05AC0, 0x0AB60, 0x096D5, 0x092E0,  # 1990
    0x0C960, 0x0D954, 0x0D4A0, 0x0DA50, 0x07552, 0x056A0, 0x0ABB7, 0x025D0, 0x092D0, 0x0CAB5,  # 2000
    0x0A950, 0x0B4A0, 0x0BAA4, 0x0AD50, 0x055D9, 0x04BA0, 0x0A5B0, 0x15176, 0x052B0, 0x0A930,  # 2010
    0x07954, 0x06AA0, 0x0AD50, 0x05B52, 0x04B60, 0x0A6E6, 0x0A4E0, 0x0D260, 0x0EA65, 0x0D530,  # 2020
    0x05AA0, 0x076A3, 0x096D0, 0x04AFB, 0x04AD0, 0x0A4D0, 0x1D0B6, 0x0D250, 0x0D520, 0x0DD45,  # 2030
    0x0B5A0, 0x056D0, 0x055B2, 0x049B0, 0x0A577, 0x0A4B0, 0x0AA50, 0x1B255, 0x06D20, 0x0ADA0,  # 2040
    0x14B63, 0x09370, 0x049F8, 0x04970, 0x064B0, 0x168A6, 0x0EA50, 0x06B20, 0x1A6C4, 0x0AAE0,  # 2050
    0x0A2E0, 0x0D2E3, 0x0C960, 0x0D557, 0x0D4A0, 0x0DA50, 0x05D55, 0x056A0, 0x0A6D0, 0x055D4,  # 2060
    0x052D0, 0x0A9B8, 0x0A950, 0x0B4A0, 0x0B6A6, 0x0AD50, 0x055A0, 0x0ABA4, 0x0A5B0, 0x052B0,  # 2070
    0x0B273, 0x06930, 0x07337, 0x06AA0, 0x0AD50, 0x14B55, 0x04B60, 0x0A570, 0x054E4, 0x0D160,  # 2080
    0x0E968, 0x0D520, 0x0DAA0, 0x16AA6, 0x056D0, 0x04AE0, 0x0A9D4, 0x0A2D0, 0x0D150, 0x0F252,  # 2090
    0x0D520,  # 2100
]

MONTH_NAMES = ("正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊")
DAY_NAMES = (
    "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
)
STEMS = "甲乙丙丁戊己庚辛壬癸"
BRANCHES = "子丑寅卯辰巳午未申酉戌亥"
ZODIACS = "鼠牛虎兔龙蛇马羊猴鸡狗猪"


class LunarError(ValueError):
    """农历换算不成立。"""


class LunarRangeError(LunarError):
    """超出 1900–2100：表里没有的年份不猜。"""


def _info(year: int) -> int:
    if not (MIN_YEAR <= year <= MAX_YEAR):
        raise LunarRangeError(f"农历只支持 {MIN_YEAR}–{MAX_YEAR} 年，{year} 不在表里")
    return LUNAR_INFO[year - MIN_YEAR]


def leap_month(year: int) -> int:
    """闰几月；0 表示这一年没有闰月。"""
    return _info(year) & 0xF


def leap_days(year: int) -> int:
    """闰月有多少天；没有闰月返回 0。"""
    if not leap_month(year):
        return 0
    return 30 if _info(year) & 0x10000 else 29


def month_days(year: int, month: int) -> int:
    """普通某月（不区分闰）的天数。"""
    if not 1 <= month <= 12:
        raise LunarError(f"月份要在 1–12 之间：{month}")
    return 30 if _info(year) & (0x10000 >> month) else 29


def year_days(year: int) -> int:
    info = _info(year)
    total = 0
    mask = 0x8000
    for _ in range(12):
        total += 30 if info & mask else 29
        mask >>= 1
    return total + leap_days(year)


def lunar_year_months(year: int) -> list[tuple[int, bool]]:
    """这一农历年的月序：[(月份, 是否闰月)]，闰月紧跟在同名月之后。"""
    rows: list[tuple[int, bool]] = []
    leap = leap_month(year)
    for month in range(1, 13):
        rows.append((month, False))
        if month == leap:
            rows.append((month, True))
    return rows


def solar_to_lunar(solar: date) -> tuple[int, int, int, bool]:
    """公历日期 → (农历年, 月, 日, 是否闰月)。"""
    if solar < BASE_SOLAR:
        raise LunarRangeError(f"{solar} 早于 {BASE_SOLAR}，表里没有")
    offset = (solar - BASE_SOLAR).days
    year = MIN_YEAR
    while year <= MAX_YEAR:
        span = year_days(year)
        if offset < span:
            break
        offset -= span
        year += 1
    if year > MAX_YEAR:
        raise LunarRangeError(f"{solar} 超出了 {MAX_YEAR} 年年底")
    for month, is_leap in lunar_year_months(year):
        span = leap_days(year) if is_leap else month_days(year, month)
        if offset < span:
            return year, month, offset + 1, is_leap
        offset -= span
    raise LunarError(f"{solar} 落在农历 {year} 年的边界外（表可能坏了）")


def lunar_to_solar(year: int, month: int, day: int, is_leap: bool = False) -> date:
    """农历月日 → 公历日期。闰月写错（那一年没有这个闰月）直接抛。"""
    if not 1 <= month <= 12:
        raise LunarError(f"月份要在 1–12 之间：{month}")
    if is_leap and leap_month(year) != month:
        raise LunarError(f"农历 {year} 年没有闰{month}月")
    limit = leap_days(year) if is_leap else month_days(year, month)
    if not 1 <= day <= limit:
        name = ("闰" if is_leap else "") + f"{month}月"
        raise LunarError(f"农历 {year} 年{name}只有 {limit} 天，没有 {day} 日")
    offset = 0
    for ly in range(MIN_YEAR, year):
        offset += year_days(ly)
    for mm, leap in lunar_year_months(year):
        if mm == month and leap == is_leap:
            break
        offset += leap_days(year) if leap else month_days(year, mm)
    return BASE_SOLAR + timedelta(days=offset + day - 1)


def ganzhi(year: int) -> str:
    """农历年的干支（如「丙午」）。"""
    index = (year - 4) % 60
    return STEMS[index % 10] + BRANCHES[index % 12]


def zodiac(year: int) -> str:
    return ZODIACS[(year - 4) % 12]


def month_name(month: int, is_leap: bool = False) -> str:
    return ("闰" if is_leap else "") + MONTH_NAMES[month - 1] + "月"


def day_name(day: int) -> str:
    if not 1 <= day <= 30:
        raise LunarError(f"日期要在 1–30 之间：{day}")
    return DAY_NAMES[day - 1]


def describe(solar: date) -> str:
    """给状态页/提示词用的一句话：「农历 八月十五（丙午年）」这种。"""
    year, month, day, is_leap = solar_to_lunar(solar)
    return f"农历{month_name(month, is_leap)}{day_name(day)}（{ganzhi(year)}{zodiac(year)}年）"


__all__ = [
    "MIN_YEAR",
    "MAX_YEAR",
    "BASE_SOLAR",
    "LUNAR_INFO",
    "LunarError",
    "LunarRangeError",
    "leap_month",
    "leap_days",
    "month_days",
    "year_days",
    "lunar_year_months",
    "solar_to_lunar",
    "lunar_to_solar",
    "ganzhi",
    "zodiac",
    "month_name",
    "day_name",
    "describe",
]
