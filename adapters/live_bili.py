"""直播只读适配器（M9-E）：用 B站公开的直播间状态接口，拿一次就走。

三条边界，写在代码里而不是文档里：

1. **不新建联网通道**：真正发请求的是 `web_http.WebHTTPAdapter.fetch_json`——那条
   通道已经有逐跳私网/元数据地址校验。这里只负责拼 URL、校验房间号、把结果压成快照。
2. **不拿主人账号**：不带任何登录态（不读浏览器的登录信息、不带鉴权头、不带自家令牌）；
   房间号只允许数字，防止有人拿它当 SSRF 的跳板去拼别的东西。
3. **只读公开状态**：标题、分区、开播状态、在线人数、关注数。弹幕/礼物/观众一概不取。

拿不到就如实抛 `LiveUnavailable`，绝不返回编造的快照。
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlencode

try:  # 包内导入
    from .web_http import WebHTTPAdapter, WebRequest
    from ..ports.live import (
        LiveError,
        LiveQuery,
        LiveSnapshot,
        LiveUnavailable,
        failed,
        normalize_snapshot,
    )
except ImportError:  # 顶层兜底
    from astrbot_plugin_feiyu_companion.adapters.web_http import WebHTTPAdapter, WebRequest
    from astrbot_plugin_feiyu_companion.ports.live import (
        LiveError,
        LiveQuery,
        LiveSnapshot,
        LiveUnavailable,
        failed,
        normalize_snapshot,
    )

LIVE_API = "https://api.live.bilibili.com/room/v1/Room/get_info"
LIVE_DEFAULT_LIMIT = 5
LIVE_DEFAULT_COOLDOWN = 60


def live_enabled_from_config(cfg: dict | None) -> bool:
    value = (cfg or {}).get("live_enable")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def live_daily_limit_from_config(cfg: dict | None, *, default: int = LIVE_DEFAULT_LIMIT) -> int:
    try:
        value = int((cfg or {}).get("live_daily_limit", default))
    except (TypeError, ValueError):
        return default
    return max(1, min(50, value))


def live_cooldown_from_config(cfg: dict | None, *, default: int = LIVE_DEFAULT_COOLDOWN) -> int:
    try:
        value = int((cfg or {}).get("live_cooldown_seconds", default))
    except (TypeError, ValueError):
        return default
    return max(0, min(3600, value))


class LiveBiliAdapter:
    """一次一份的直播间状态源。

    `fetch_json` 可以注入（测试用），缺省才惰性建一个真的 `WebHTTPAdapter`。
    """

    port_name = "LivePortV1"

    def __init__(
        self,
        *,
        fetch_json: Callable[..., Any] | None = None,
        enabled: bool = False,
        logger: Any = None,
        clock: Any = None,
    ) -> None:
        self._fetch = fetch_json
        self.enabled = bool(enabled)
        self.logger = logger
        self.clock = clock
        self.calls = 0
        self.last_error = ""

    # ---- 静态面 -------------------------------------------------------

    def available(self) -> tuple[bool, str]:
        """只看配置，不发请求。"""
        if not self.enabled:
            return False, "直播只读关着（live_enable=false）"
        return True, "直播只读已就绪（B站公开状态接口，不登录）"

    def status(self) -> tuple[str, str]:
        ok, why = self.available()
        return ("read_only" if ok else "integration_pending"), why

    def info(self) -> dict[str, Any]:
        state, detail = self.status()
        return {
            "port": self.port_name,
            "state": state,
            "detail": detail,
            "enabled": self.enabled,
            "calls": self.calls,
            "last_error": self.last_error,
            "api": LIVE_API,
        }

    # ---- 取一份 -------------------------------------------------------

    def snapshot(self, query: LiveQuery) -> LiveSnapshot:
        raise LiveUnavailable("直播状态只能异步取：请用 snapshot_async")

    async def snapshot_async(self, query: LiveQuery) -> LiveSnapshot:
        ok, why = self.available()
        if not ok:
            raise LiveUnavailable(f"integration_pending：{why}")
        if not isinstance(query, LiveQuery):
            raise LiveError("要一个 LiveQuery")

        url = f"{LIVE_API}?{urlencode({'room_id': int(query.room_id)})}"
        try:
            raw = await self._call(url)
        except LiveUnavailable:
            raise
        except LiveError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            raise LiveUnavailable(
                f"直播接口连不上（{type(exc).__name__}）：网线还是那条，只是这次没通"
            ) from None

        snapshot = normalize_snapshot(raw, query, now=self._now())
        self.calls += 1
        return snapshot

    async def _call(self, url: str) -> Any:
        fetcher = self._fetch
        if fetcher is None:
            fetcher = WebHTTPAdapter(enabled=True, logger=self.logger)
            self._fetch = fetcher
        if hasattr(fetcher, "fetch_json"):
            # 复用读页通道：它的 WebRequest 已经带好「只读、限长」的语义
            request = WebRequest(url=url, scene="private", max_chars=2000, max_bytes=200_000)
            result = fetcher.fetch_json(request)
        else:  # 注入的协程函数（测试用）：给 URL 就行
            result = fetcher(url)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, dict):
            return result
        if isinstance(result, tuple) and result:
            body, web_result = result[0], (result[1] if len(result) > 1 else None)
            if body is None:
                reason = getattr(web_result, "reason", "") or "接口没回可用的 JSON"
                raise LiveUnavailable(f"直播接口没回可用的 JSON：{reason}")
            return body
        raise LiveUnavailable("直播接口回的东西看不懂")

    def _now(self):
        if self.clock is not None and hasattr(self.clock, "now"):
            return self.clock.now()
        from datetime import datetime, timedelta, timezone

        return datetime.now(timezone(timedelta(hours=8)))


__all__ = [
    "LIVE_API",
    "LiveBiliAdapter",
    "live_enabled_from_config",
    "live_daily_limit_from_config",
    "live_cooldown_from_config",
]
