"""M8-A：一次「去网上看一眼」的完整判断（网页 + 天气）。

天气不是一个单独的联网能力：它就是「读一个只读 JSON 接口」，
所以共用同一个 WebPort，只是把结果整理成人话。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from ..domain.context import SCENE_PRIVATE
from ..domain.delivery import Delivery, KIND_TEXT, ensure_deliverable
from ..domain.grants import CAP_TEXT, CAP_WEB, CapabilityDenied
from ..ports.web import (
    DEFAULT_MAX_CHARS,
    DEFAULT_TIMEOUT,
    WebContractError,
    WebRequest,
    WebResult,
    parse_target,
)

DEFAULT_DAILY_LIMIT = 5
WEATHER_API = "https://api.open-meteo.com/v1/forecast"
WEATHER_DAYS = 3


class WebTurnError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebPlan:
    ok: bool
    reason: str = ""
    request: WebRequest | None = None
    url: str = ""
    delivery: Delivery | None = None


@dataclass(frozen=True)
class WebOutcome:
    ok: bool
    reason: str = ""
    text: str = ""
    url: str = ""
    chars: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def needs_fallback(self) -> bool:
        return not self.ok


def _digest(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


class WebTurn:
    """只回答「这一次能不能去看」，不自己发请求。"""

    def __init__(self, policy, *, max_chars: int = DEFAULT_MAX_CHARS,
                 daily_limit: int = DEFAULT_DAILY_LIMIT, clock=None) -> None:
        self.policy = policy
        self.max_chars = int(max_chars)
        self.daily_limit = max(0, int(daily_limit))
        self.clock = clock
        self._day = ""
        self._used = 0

    # ---- 计数 -------------------------------------------------------
    def _now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now()
        return datetime.now()

    def _roll(self) -> None:
        day = self._now().strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self._used = 0

    def used_today(self) -> int:
        self._roll()
        return self._used

    def remaining_today(self) -> int:
        return max(0, self.daily_limit - self.used_today())

    # ---- 计划 -------------------------------------------------------
    def plan(self, ctx, target: str, *, now=None) -> WebPlan:
        if getattr(ctx, "scene", "") != SCENE_PRIVATE:
            return WebPlan(False, "网页只在私聊里看：群里不替你联网")
        if not getattr(ctx, "is_owner", False):
            return WebPlan(False, "只给主人自己用")
        if not getattr(ctx, "is_bound", False):
            return WebPlan(False, "这个会话没绑定主人，不联网")
        try:
            url, _host = parse_target(target)
        except WebContractError as exc:
            return WebPlan(False, str(exc))
        if self.remaining_today() <= 0:
            return WebPlan(False, f"今天已经看了 {self.daily_limit} 次（上限 {self.daily_limit} 次），明天再说")
        try:
            grant = self.policy.grant_once(
                CAP_WEB, scene=getattr(ctx, "scene", SCENE_PRIVATE), reason="这一句明确要看一个网页"
            )
        except CapabilityDenied as exc:
            return WebPlan(False, str(exc))
        delivery = Delivery(
            id=f"web:{_digest(url)}",
            session_id=getattr(ctx, "session_id", ""),
            kind=KIND_TEXT,
            idem_key=f"web:{_digest(url)}",
            created_at=(now or self._now()).isoformat(timespec="seconds"),
        )
        try:
            ensure_deliverable(delivery, grant)
        except CapabilityDenied as exc:
            return WebPlan(False, str(exc))
        request = WebRequest(url=url, scene=getattr(ctx, "scene", SCENE_PRIVATE),
                            max_chars=self.max_chars, timeout=DEFAULT_TIMEOUT)
        return WebPlan(True, "", request=request, url=url, delivery=delivery)

    async def render(self, port, plan: WebPlan) -> WebOutcome:
        if not plan.ok or plan.request is None:
            return WebOutcome(False, plan.reason or "没打算去看")
        try:
            result = await port.fetch(plan.request)
        except Exception as exc:  # noqa: BLE001
            return WebOutcome(False, f"取不到：{type(exc).__name__}", url=plan.url)
        if not isinstance(result, WebResult):
            return WebOutcome(False, "端口回了看不懂的东西", url=plan.url)
        if not result.ok:
            return WebOutcome(False, result.reason or "没取到", url=plan.url)
        self._roll()
        self._used += 1
        head = result.title or plan.url
        body = result.text.strip()
        notes = []
        if result.extra.get("truncated"):
            notes.append("（太长了，只读了前一段）")
        if result.redirects:
            notes.append(f"（跳了 {len(result.redirects)} 次才到）")
        text = f"{head}\n{'-' * min(20, len(head))}\n{body}"
        text += f"\n\n来源：{result.url}" + ("".join(notes))
        return WebOutcome(True, "", text=text[: self.max_chars + 200], url=result.url,
                          chars=len(body), extra={"elapsed_ms": result.elapsed_ms})


# ---------------------------------------------------------------- 天气

def forecast_url(lat: float, lon: float, *, days: int = WEATHER_DAYS, tz: str = "Asia/Shanghai") -> str:
    """open-meteo 的只读预报接口：不要 key，不要登录。"""
    lat = max(-90.0, min(90.0, float(lat)))
    lon = max(-180.0, min(180.0, float(lon)))
    days = max(1, min(7, int(days)))
    return (
        f"{WEATHER_API}?latitude={lat:.4f}&longitude={lon:.4f}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        f"&forecast_days={days}&timezone={tz}"
    )


WEATHER_CODES = {
    0: "晴", 1: "基本晴", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨带冰雹", 99: "强雷雨带冰雹",
}


def weather_code_text(code) -> str:
    try:
        return WEATHER_CODES.get(int(code), f"天气码 {code}")
    except (TypeError, ValueError):
        return "天气不明"


def summarize_weather(payload: dict, *, place: str = "", days: int = WEATHER_DAYS) -> str:
    """把 open-meteo 的 JSON 变成三行人话；缺字段就少写一行，不编。"""
    if not isinstance(payload, dict):
        return ""
    lines: list[str] = []
    title = f"{place}的天气" if place else "那边的天气"
    current = payload.get("current") or {}
    if isinstance(current, dict) and current.get("temperature_2m") is not None:
        temp = current.get("temperature_2m")
        feels = current.get("apparent_temperature")
        code = weather_code_text(current.get("weather_code"))
        humidity = current.get("relative_humidity_2m")
        wind = current.get("wind_speed_10m")
        line = f"{title}：现在 {code}，{temp}°C"
        if feels is not None and abs(float(feels) - float(temp)) >= 1.0:
            line += f"（体感 {feels}°C）"
        extra = []
        if humidity is not None:
            extra.append(f"湿度 {humidity}%")
        if wind is not None:
            extra.append(f"风速 {wind} km/h")
        if extra:
            line += "，" + "、".join(extra)
        lines.append(line + "。")
    daily = payload.get("daily") or {}
    if isinstance(daily, dict) and isinstance(daily.get("time"), list):
        times = daily["time"]
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        codes = daily.get("weather_code") or []
        rains = daily.get("precipitation_probability_max") or []
        parts = []
        for index, stamp in enumerate(times[: max(1, min(int(days), 7))]):
            label = str(stamp)[5:] if len(str(stamp)) >= 10 else str(stamp)
            piece = f"{label}"
            if index < len(highs) and highs[index] is not None and index < len(lows) and lows[index] is not None:
                piece += f" {lows[index]}~{highs[index]}°C"
            if index < len(codes) and codes[index] is not None:
                piece += f" {weather_code_text(codes[index])}"
            if index < len(rains) and rains[index] is not None:
                piece += f"（下雨 {rains[index]}%）"
            parts.append(piece)
        if parts:
            lines.append("接下来：" + "；".join(parts) + "。")
    if not lines:
        return ""
    return "\n".join(lines)


__all__ = [
    "DEFAULT_DAILY_LIMIT",
    "WEATHER_API",
    "WebTurn",
    "WebPlan",
    "WebOutcome",
    "WebTurnError",
    "forecast_url",
    "summarize_weather",
    "weather_code_text",
]
