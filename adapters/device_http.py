"""设备只读适配器（M9-D）：走和识屏同一条出站隧道，读一次本机读数。

和 `screen_http.py` 的关系：**共用同一把令牌、同一个地址、同一条隧道**，
地址合法性的判断也直接复用它的 `check_base_url`（一处规则，两个端口不各写一套）。

三件事刻意不做：
- 不落盘（读数只在内存里过一手）；
- 不发第二次请求去"补全"（缺字段就让端口那边标 note）；
- 不解释、不建议——只把数字交出去，怎么说由 `ports.device.describe` 决定。
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable

from ..ports.device import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    DeviceError,
    DeviceRequest,
    DeviceSnapshot,
    DeviceUnavailable,
    normalize_snapshot,
)
from .screen_http import check_base_url

AGENT_NAME = "feiyu-screen-agent"
DEFAULT_BASE = "http://172.17.0.1:18189"
DEVICES_PATH = "/devices"
DEFAULT_TIMEOUT = 15.0
DEFAULT_LABEL = "本机"

Getter = Callable[[str, dict[str, str], float], Any]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "开")


def device_enabled_from_config(cfg: dict) -> bool:
    return _truthy((cfg or {}).get("device_enable"))


def device_label_from_config(cfg: dict) -> str:
    label = str((cfg or {}).get("device_label") or "").strip()
    return label[:40] or DEFAULT_LABEL


def device_daily_limit_from_config(cfg: dict, default: int = 10) -> int:
    try:
        return max(1, min(50, int((cfg or {}).get("device_daily_limit", default))))
    except (TypeError, ValueError):
        return default


def device_cooldown_from_config(cfg: dict, default: int = 60) -> int:
    try:
        return max(0, min(3600, int((cfg or {}).get("device_cooldown_seconds", default))))
    except (TypeError, ValueError):
        return default


def device_ttl_from_config(cfg: dict, default: int = DEFAULT_TTL_SECONDS) -> int:
    try:
        value = int((cfg or {}).get("device_ttl_seconds", default))
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_TTL_SECONDS, value))


class DeviceHTTPAdapter:
    """从本机 agent 的 `/devices` 读一次。地址只认回环/私网，令牌走请求头。"""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE,
        token: str = "",
        enabled: bool = False,
        label: str = DEFAULT_LABEL,
        timeout: float = DEFAULT_TIMEOUT,
        ttl: int = DEFAULT_TTL_SECONDS,
        get: Getter | None = None,
        logger: Any = None,
        clock: Any = None,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE).strip().rstrip("/")
        self.token = str(token or "")
        self.enabled = bool(enabled)
        self.label = str(label or DEFAULT_LABEL)[:40] or DEFAULT_LABEL
        self.timeout = float(timeout)
        self.ttl = max(1, min(MAX_TTL_SECONDS, int(ttl)))
        self._get = get
        self._logger = logger
        self._clock = clock
        self.calls = 0
        self.last_error = ""

    # ---- 只读自述 ----
    def available(self) -> tuple[bool, str]:
        """只看配置、**不发请求**。"""
        if not self.enabled:
            return False, "设备读数关着（device_enable=false）：默认就是关的，一条都不读"
        ok, why = check_base_url(self.base_url)
        if not ok:
            return False, why
        if len(self.token) < 16:
            return False, "没配 screen_agent_token（或太短）：连不上本机采集端"
        return True, f"本机采集端 {self.base_url}{DEVICES_PATH}（只读一次，读完即过期）"

    def status(self) -> tuple[str, str]:
        ok, why = self.available()
        return ("read_only", why) if ok else ("integration_pending", why)

    def info(self) -> dict[str, Any]:
        return {
            "port": "DevicePortV1",
            "base_url": self.base_url,
            "enabled": self.enabled,
            "label": self.label,
            "calls": self.calls,
            "last_error": self.last_error,
        }

    # ---- 读一次 ----
    def snapshot(self, request: DeviceRequest | None = None) -> DeviceSnapshot:
        raise DeviceUnavailable("设备读数只能异步取：请用 snapshot_async")

    async def snapshot_async(self, request: DeviceRequest | None = None) -> DeviceSnapshot:
        request = request or DeviceRequest(ttl_seconds=self.ttl)
        ok, why = self.available()
        if not ok:
            raise DeviceUnavailable(why)

        url = f"{self.base_url}{DEVICES_PATH}?ttl={int(request.ttl_seconds)}"
        headers = {"X-Screen-Token": self.token, "Accept": "application/json"}
        try:
            raw = self._get(url, headers, self.timeout) if self._get else self._aiohttp_get(
                url, headers, self.timeout
            )
            if inspect.isawaitable(raw):
                raw = await raw
        except DeviceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}"
            raise DeviceUnavailable(
                f"采集端连不上（{type(exc).__name__}）：隧道是不是没起？"
            ) from exc

        self.calls += 1
        if not isinstance(raw, dict):
            raise DeviceUnavailable("采集端回的东西不是对象")
        if raw.get("ok") is False:
            reason = str(raw.get("reason") or "采集端没说原因")
            self.last_error = reason[:120]
            raise DeviceUnavailable(reason)
        try:
            snapshot = normalize_snapshot(raw, request)
        except DeviceError as exc:
            self.last_error = str(exc)[:120]
            raise DeviceUnavailable(f"采集端回的形状不对：{exc}") from exc
        if not snapshot.device:
            snapshot = _with_device(snapshot, self.label)
        return snapshot

    async def _aiohttp_get(self, url: str, headers: dict[str, str], timeout: float) -> dict:
        # aiohttp 只在真的要联网时才 import（延迟导入，测试里不会碰网络模块）
        import aiohttp

        client_timeout = aiohttp.ClientTimeout(total=timeout, connect=3)
        async with aiohttp.ClientSession(
            timeout=client_timeout, trust_env=False
        ) as session:
            async with session.get(url, headers=headers) as response:
                text = await response.text()
                if response.status != 200:
                    raise DeviceUnavailable(f"采集端回 HTTP {response.status}：{text[:80]}")
                import json

                return json.loads(text)


def _with_device(snapshot: DeviceSnapshot, label: str) -> DeviceSnapshot:
    from dataclasses import replace

    return replace(snapshot, device=label)


__all__ = [
    "AGENT_NAME",
    "DEFAULT_BASE",
    "DEVICES_PATH",
    "DEFAULT_LABEL",
    "DeviceHTTPAdapter",
    "device_enabled_from_config",
    "device_label_from_config",
    "device_daily_limit_from_config",
    "device_cooldown_from_config",
    "device_ttl_from_config",
]
