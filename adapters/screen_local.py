"""M9-A：识屏适配器。

现实是：bot 跑在云上的容器里，容器没有你的桌面。所以这一版**没有**采集端，
`status()` 会如实说「不可用」，`snapshot()` 抛 `ScreenUnavailable`——
不伪造「我看了一眼」。

采集端的形状先定死（`ScreenSource` 协议）：给它一个 `ScreenRequest`，
它交回一帧 `ScreenFrame`（设备名、窗口名、拍摄时间、过期时间、宽高、文字、遮罩框）。
等哪天本机采集端真的接上（M9-B：本机 agent + outbound 隧道），
把它的 `capture()` 塞进 `ScreenAdapter(source=...)` 就行，上层一行不用改。
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone

from ..ports.screen import (
    DEFAULT_DEVICE,
    DEFAULT_MAX_CHARS,
    DEFAULT_TTL_SECONDS,
    ScreenFrame,
    ScreenRequest,
    ScreenResult,
    ScreenUnavailable,
    failed,
    validate_frame,
)
from ..runtime.clock import SystemClock

TZ = timezone(timedelta(hours=8))
AGENT_NAME = "feiyu-screen-agent"  # 本机采集端的名字，M9-B 用


def screen_enabled_from_config(cfg) -> bool:
    raw = (cfg or {}).get("screen_enable", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "开")


def screen_daily_limit_from_config(cfg) -> int:
    try:
        return max(1, int((cfg or {}).get("screen_daily_limit", 3) or 3))
    except (TypeError, ValueError):
        return 3


def screen_cooldown_from_config(cfg) -> int:
    try:
        return max(0, int((cfg or {}).get("screen_cooldown_seconds", 120) or 120))
    except (TypeError, ValueError):
        return 120


def screen_max_chars_from_config(cfg) -> int:
    try:
        return max(200, int((cfg or {}).get("screen_max_chars", DEFAULT_MAX_CHARS) or DEFAULT_MAX_CHARS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS


def camera_enabled_from_config(cfg) -> bool:
    raw = (cfg or {}).get("camera_enable", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "开")


def camera_daily_limit_from_config(cfg) -> int:
    try:
        return max(1, int((cfg or {}).get("camera_daily_limit", 2) or 2))
    except (TypeError, ValueError):
        return 2


def camera_cooldown_from_config(cfg) -> int:
    try:
        return max(0, int((cfg or {}).get("camera_cooldown_seconds", 180) or 180))
    except (TypeError, ValueError):
        return 180


def camera_device_from_config(cfg) -> str:
    return str((cfg or {}).get("camera_device", "") or "").strip()


def screen_ttl_from_config(cfg) -> int:
    try:
        return max(1, int((cfg or {}).get("screen_ttl_seconds", DEFAULT_TTL_SECONDS) or DEFAULT_TTL_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS


class ScreenSource:
    """本机采集端要实现的形状（这一版没有实现者）。"""

    name = AGENT_NAME

    def capture(self, request: ScreenRequest) -> ScreenFrame:  # pragma: no cover - 协议
        raise NotImplementedError

    def available(self) -> tuple[bool, str]:
        return False, "没有采集端"


class ScreenAdapter:
    """把「要不要看、能不能看、看到的是什么」分开：这里只管能不能。"""

    def __init__(self, *, enabled: bool = False, source=None, logger=None, clock=None):
        self.enabled = bool(enabled)
        self.source = source
        self.logger = logger
        self.clock = clock or SystemClock()
        self.calls = 0
        self.last_error = ""

    # ---- 状态 -------------------------------------------------------
    def status(self) -> tuple[str, str]:
        if not self.enabled:
            return (
                "integration_pending",
                "识屏关着（screen_enable=false）：默认就是关的，一条都不看",
            )
        if self.source is None:
            return (
                "integration_pending",
                "还没有本机采集端：她在云上，读不到你桌面的像素——这一条不会假装看见了",
            )
        try:
            ok, why = self.source.available()
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            return ("integration_pending", f"采集端问不通（{type(exc).__name__}）")
        if not ok:
            return ("integration_pending", why or "采集端说不可用")
        return ("read_only", f"本机采集端就绪（{getattr(self.source, 'name', AGENT_NAME)}）：一次一帧，看完即过期")

    def info(self) -> dict:
        state, detail = self.status()
        return {
            "state": state,
            "detail": detail,
            "enabled": self.enabled,
            "source": getattr(self.source, "name", "") if self.source else "",
            "calls": int(self.calls),
            "last_error": self.last_error,
        }

    # ---- 取一帧 -----------------------------------------------------
    def _precheck(self) -> ScreenResult | None:
        if not self.enabled:
            return failed("识屏关着（screen_enable=false）")
        if self.source is None:
            raise ScreenUnavailable(
                "integration_pending：没有本机采集端，读不到你桌面的像素"
            )
        return None

    def _finish(self, frame, req: ScreenRequest) -> ScreenResult:
        self.calls += 1
        now = self.clock.now()
        try:
            checked = validate_frame(frame, req, now=now)
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            return failed(str(exc))
        if not checked.ok:
            return failed(checked.reason or "采集端说这一帧不成立")
        extra = {
            "target": checked.target,
            "width": checked.width,
            "height": checked.height,
        }
        image_path = str(checked.extra.get("image_path") or "")
        if image_path:
            extra["image_path"] = image_path
        image_bytes = int(checked.extra.get("image_bytes", 0) or 0)
        if image_bytes:
            extra["image_bytes"] = image_bytes
        return ScreenResult(
            ok=True,
            text=checked.text,
            device=checked.device,
            window=checked.window,
            captured_at=checked.captured_at,
            expires_at=checked.expires_at,
            redactions=int(checked.extra.get("redactions", 0) or 0),
            extra=extra,
        )

    def _collect_error(self, exc: Exception) -> ScreenResult:
        """采集端的「不可用/对端拒绝」要原话带给用户；其它异常只说类型。"""
        self.last_error = type(exc).__name__
        if isinstance(exc, ScreenUnavailable):
            return failed(str(exc))
        return failed(f"采集失败（{type(exc).__name__}），这一眼没看成")

    def snapshot(self, request: ScreenRequest | None = None) -> ScreenResult:
        req = request or ScreenRequest()
        early = self._precheck()
        if early is not None:
            return early
        try:
            frame = self.source.capture(req)
        except Exception as exc:  # noqa: BLE001
            return self._collect_error(exc)
        return self._finish(frame, req)

    async def snapshot_async(self, request: ScreenRequest | None = None) -> ScreenResult:
        """异步版：采集端给了 `capture_async` 就用它，否则把同步 `capture` 丢到线程里。"""
        req = request or ScreenRequest()
        early = self._precheck()
        if early is not None:
            return early
        capture_async = getattr(self.source, "capture_async", None)
        try:
            if callable(capture_async):
                frame = capture_async(req)
                if inspect.isawaitable(frame):
                    frame = await frame
            else:
                frame = await asyncio.to_thread(self.source.capture, req)
        except Exception as exc:  # noqa: BLE001
            return self._collect_error(exc)
        return self._finish(frame, req)


__all__ = [
    "AGENT_NAME",
    "ScreenSource",
    "ScreenAdapter",
    "screen_enabled_from_config",
    "screen_daily_limit_from_config",
    "screen_cooldown_from_config",
    "screen_max_chars_from_config",
    "screen_ttl_from_config",
    "camera_enabled_from_config",
    "camera_daily_limit_from_config",
    "camera_cooldown_from_config",
    "camera_device_from_config",
]
