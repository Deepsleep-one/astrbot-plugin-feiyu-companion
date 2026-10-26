"""M9-B：真·本机采集端的云端一侧——通过出站隧道取一帧。

链路：云上容器 → `http://172.17.0.1:18189`（宿主机上的小中继）→ 宿主 `127.0.0.1:18189`
→ `ssh -R` 反向隧道 → **你自己电脑上**的 `screen_agent/agent.py`。

几条硬规矩（都写进测试）：
1. 采集端只交一帧、不落盘；云端把这一帧存到 `state_dir/screen/` 供发图用。
2. token 走 `X-Screen-Token` 头，不写 URL、不进日志。
3. 采集端回 `ok=false` 就如实说「没看成」，不编造「看见了」。
4. 地址只接受 http(s)、不带账号密码、且必须是回环或私网——不往公网发截屏请求。
5. `aiohttp` 延迟导入：没装或用不上时不会在 import 阶段炸。
"""
from __future__ import annotations

import base64
import binascii
import inspect
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ..ports.screen import (
    DEFAULT_DEVICE,
    ScreenFrame,
    ScreenRequest,
    ScreenUnavailable,
)
from ..runtime.clock import SystemClock

TZ = timezone(timedelta(hours=8))
AGENT_NAME = "feiyu-screen-agent"
DEFAULT_BASE = "http://172.17.0.1:18189"
CAPTURE_PATH = "/capture"
HEALTH_PATH = "/health"
DEFAULT_TIMEOUT = 15.0
MAX_PNG_BYTES = 6 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_BLOCKED_HOSTS = ("", "0.0.0.0", "::")


def _truthy(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on", "开")


def screen_agent_url_from_config(cfg) -> str:
    raw = str((cfg or {}).get("screen_agent_url", "") or "").strip()
    return raw or DEFAULT_BASE


def screen_agent_token_from_config(cfg) -> str:
    return str((cfg or {}).get("screen_agent_token", "") or "").strip()


def screen_describe_from_config(cfg) -> bool:
    return _truthy((cfg or {}).get("screen_describe", False))


def screen_timeout_from_config(cfg) -> float:
    try:
        value = float((cfg or {}).get("screen_timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(3.0, min(60.0, value))


def _host_is_local(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    if host.startswith("10.") or host.startswith("192.168."):
        return True
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return True
    if host.startswith(("fc", "fd", "fe80")):
        return True
    return False


def check_base_url(base_url: str) -> tuple[bool, str]:
    """回环/私网 + 无账号密码 + http(s)。公网一律拒。"""
    text = str(base_url or "").strip()
    if not text:
        return False, "没有配 screen_agent_url"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        return False, f"只接受 http/https，收到的是 {parts.scheme or '空'}"
    if "@" in (parts.netloc or ""):
        return False, "地址里不要带账号密码"
    host = parts.hostname or ""
    if not host or host in _BLOCKED_HOSTS:
        return False, "地址里没有主机名"
    if not _host_is_local(host):
        return False, f"{host} 不是回环也不是私网：不往公网发截屏请求"
    return True, ""


class ScreenHTTPAdapter:
    """`ScreenSource` 的 HTTP 实现：问本机采集端要一帧，存到本地临时文件。"""

    name = AGENT_NAME

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE,
        token: str = "",
        enabled: bool = False,
        state_dir=None,
        timeout: float = DEFAULT_TIMEOUT,
        post=None,
        logger=None,
        clock=None,
    ):
        self.base_url = str(base_url or DEFAULT_BASE).rstrip("/")
        self.token = str(token or "")
        self.enabled = bool(enabled)
        self.state_dir = Path(state_dir) if state_dir else None
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self._post = post
        self.logger = logger
        self.clock = clock or SystemClock()
        self.calls = 0
        self.last_error = ""
        self.last_image = ""

    # ---- 可用性（只看配置，不发请求）--------------------------------
    def available(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "识屏关着（screen_enable=false）"
        ok, why = check_base_url(self.base_url)
        if not ok:
            return False, why
        if len(self.token) < 16:
            return False, "没配 screen_agent_token（或太短）：采集端不认我"
        if self.state_dir is None:
            return False, "没有可写目录放这一帧"
        return True, f"隧道地址 {self.base_url}（一次一帧，看完即过期）"

    def info(self) -> dict:
        ok, why = self.available()
        return {
            "ok": ok,
            "detail": why,
            "base_url": self.base_url,
            "calls": int(self.calls),
            "last_error": self.last_error,
            "last_image": self.last_image,
        }

    # ---- 取帧 -------------------------------------------------------
    def capture(self, request: ScreenRequest) -> ScreenFrame:  # pragma: no cover - 兜底
        raise ScreenUnavailable("HTTP 采集端只能异步取帧：请用 snapshot_async")

    async def capture_async(self, request: ScreenRequest) -> ScreenFrame:
        ok, why = self.available()
        if not ok:
            raise ScreenUnavailable(f"integration_pending：{why}")
        payload = {
            "source": request.source,
            "target": request.target,
            "device": request.device,
            "window": request.window,
            "ttl_seconds": int(request.ttl_seconds),
        }
        url = f"{self.base_url}{CAPTURE_PATH}"
        try:
            body = await self._call(url, payload)
        except ScreenUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            raise ScreenUnavailable(f"采集端连不上（{type(exc).__name__}）：隧道是不是没起？") from exc

        if not isinstance(body, dict):
            raise ScreenUnavailable("采集端回的东西不是 JSON 对象")
        if not body.get("ok"):
            raise ScreenUnavailable(str(body.get("reason") or "采集端说没看成，没给原因"))
        raw = self._decode(body.get("png_b64"))
        path = self._save(raw, body)
        self.calls += 1
        self.last_image = str(path)
        return ScreenFrame(
            ok=True,
            reason="",
            target=str(body.get("target") or request.target),
            source=str(body.get("source") or request.source),
            device=str(body.get("device") or DEFAULT_DEVICE),
            window=str(body.get("window") or request.window),
            captured_at=str(body.get("captured_at") or ""),
            expires_at=str(body.get("expires_at") or ""),
            ttl_seconds=int(body.get("ttl_seconds") or request.ttl_seconds),
            width=int(body.get("width") or 0),
            height=int(body.get("height") or 0),
            text="",  # 本机没有 OCR：一个字都不编
            boxes=(),
            extra={"image_path": str(path), "image_bytes": len(raw)},
        )

    # ---- 内部 -------------------------------------------------------
    async def _call(self, url: str, payload: dict) -> dict:
        headers = {"X-Screen-Token": self.token, "Content-Type": "application/json"}
        post = self._post or self._aiohttp_post
        result = post(url, payload, headers, self.timeout)
        if inspect.isawaitable(result):
            result = await result
        return result

    @staticmethod
    async def _aiohttp_post(url: str, payload: dict, headers: dict, timeout: float) -> dict:
        import aiohttp  # 延迟导入：只有真取帧时才需要

        client_timeout = aiohttp.ClientTimeout(total=timeout, connect=3)
        async with aiohttp.ClientSession(timeout=client_timeout, trust_env=False) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                raw = await resp.read()
                if resp.status != 200:
                    raise ScreenUnavailable(f"采集端回了 {resp.status}")
                import json

                return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _decode(png_b64) -> bytes:
        text = str(png_b64 or "")
        if not text:
            raise ScreenUnavailable("采集端没给图")
        try:
            raw = base64.b64decode(text, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ScreenUnavailable(f"图解码失败（{type(exc).__name__}）") from exc
        if not raw.startswith(PNG_MAGIC):
            raise ScreenUnavailable("回来的不是 PNG")
        if len(raw) > MAX_PNG_BYTES:
            raise ScreenUnavailable(f"图太大（{len(raw)} 字节，上限 {MAX_PNG_BYTES}）")
        return raw

    def _save(self, raw: bytes, body: dict) -> Path:
        if self.state_dir is None:
            raise ScreenUnavailable("没有可写目录放这一帧")
        target_dir = Path(self.state_dir) / "screen"
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
        width = int(body.get("width") or 0)
        height = int(body.get("height") or 0)
        fd, tmp = tempfile.mkstemp(dir=str(target_dir), prefix=".frame-", suffix=".png")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
            final = target_dir / f"frame_{stamp}_{width}x{height}.png"
            os.replace(tmp, final)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return final


__all__ = [
    "AGENT_NAME",
    "DEFAULT_BASE",
    "MAX_PNG_BYTES",
    "ScreenHTTPAdapter",
    "check_base_url",
    "screen_agent_token_from_config",
    "screen_agent_url_from_config",
    "screen_describe_from_config",
    "screen_timeout_from_config",
]
