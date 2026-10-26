"""M8-A：网页端口的真实实现（全项目第二个、也是最后一个允许联网的地方）。

规矩：
- 只有 GET；
- 不跟随自动重定向：每一跳都重新过 `parse_target`，私网/回环一律断；
- 读多少字节有上限，超了就截断并说明；
- 除了这个文件，别处不许出现 aiohttp（见 selftest_web 的静态检查）。
"""
from __future__ import annotations

import time
from urllib.parse import urljoin

from ..ports.web import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_CHARS,
    DEFAULT_TIMEOUT,
    MAX_REDIRECTS,
    WebContractError,
    WebRequest,
    WebResult,
    WebUnavailable,
    failed,
    html_title,
    html_to_text,
    parse_target,
)

DEFAULT_USER_AGENT = "feiyu-companion/0.5 (+read-only; one page per request)"


def web_enabled_from_config(cfg: dict | None) -> bool:
    value = (cfg or {}).get("web_enable", False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _float(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


class WebHTTPAdapter:
    """一个 URL 一次请求。失败只写类型，不回显正文。"""

    name = "web_http"

    def __init__(self, *, enabled: bool = False, logger=None) -> None:
        self.enabled = bool(enabled)
        self.logger = logger
        self.calls = 0
        self.last_error = ""

    # ---- 状态 -------------------------------------------------------
    def status(self) -> tuple[str, str]:
        if not self.enabled:
            return "integration_pending", "网页读取关着（web_enable=false）：这一版不会替你去外面取东西"
        try:
            import aiohttp  # noqa: F401
        except Exception:  # noqa: BLE001
            return "integration_pending", "环境里没有 aiohttp：网页读取不可用"
        return "read_only", "只读网页读取已就绪（http/https 单页、逐跳校验、不碰私网）"

    def _log(self, fmt: str, *args) -> None:
        if self.logger is not None:
            try:
                self.logger.info(fmt, *args)
            except Exception:  # noqa: BLE001
                pass

    # ---- 读取 -------------------------------------------------------
    async def _read(self, request: WebRequest) -> tuple[WebResult, bytes]:
        """执行一次「最多 MAX_REDIRECTS 跳」的 GET，返回 (结果, 原始字节)。"""
        try:
            import aiohttp
        except Exception as exc:  # noqa: BLE001
            raise WebUnavailable("环境里没有 aiohttp") from exc

        started = time.monotonic()
        current, _ = parse_target(request.url)
        redirects: list[str] = []
        timeout = aiohttp.ClientTimeout(total=max(1.0, float(request.timeout or DEFAULT_TIMEOUT)))
        max_bytes = max(4096, int(request.max_bytes or DEFAULT_MAX_BYTES))

        connector = aiohttp.TCPConnector(limit=2, ssl=True)
        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            trust_env=False,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.5"},
        ) as session:
            for hop in range(MAX_REDIRECTS + 1):
                async with session.get(current, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        if not location:
                            return failed("对端给了重定向但没给地址", url=current), b""
                        if hop >= MAX_REDIRECTS:
                            return failed(f"重定向超过 {MAX_REDIRECTS} 跳", url=current), b""
                        nxt = urljoin(current, location)
                        try:
                            parse_target(nxt)
                        except WebContractError as exc:
                            return failed(f"重定向到不该去的地方：{exc}", url=current), b""
                        redirects.append(nxt)
                        current = nxt
                        continue

                    if resp.status >= 400:
                        return failed(f"对端返回 {resp.status}", url=current), b""

                    chunks: list[bytes] = []
                    total = 0
                    truncated = False
                    async for chunk in resp.content.iter_chunked(65536):
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= max_bytes:
                            truncated = True
                            break
                    body = b"".join(chunks)[:max_bytes]
                    result = WebResult(
                        ok=True,
                        url=current,
                        bytes_read=len(body),
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        redirects=tuple(redirects),
                        extra={"truncated": truncated, "status": resp.status},
                    )
                    return result, body

        return failed("没有拿到任何响应", url=current), b""

    async def fetch(self, request: WebRequest) -> WebResult:
        self.calls += 1
        try:
            result, body = await self._read(request)
        except WebContractError as exc:
            self.last_error = "WebContractError"
            return failed(str(exc), url=request.url)
        except WebUnavailable as exc:
            self.last_error = "WebUnavailable"
            return failed(str(exc), url=request.url)
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            self._log("[feiyu_companion] 取网页失败（%s）", type(exc).__name__)
            return failed(f"取不到：{type(exc).__name__}", url=request.url)
        if not result.ok:
            self.last_error = result.reason[:80]
            return result
        charset = "utf-8"
        raw = body.decode(charset, errors="replace")
        if not raw.strip():
            return failed("页面是空的（可能全靠脚本渲染）", url=result.url)
        return WebResult(
            ok=True,
            url=result.url,
            title=html_title(raw),
            text=html_to_text(raw, max_chars=int(request.max_chars or DEFAULT_MAX_CHARS)),
            bytes_read=result.bytes_read,
            elapsed_ms=result.elapsed_ms,
            redirects=result.redirects,
            extra=result.extra,
        )

    async def fetch_json(self, request: WebRequest) -> tuple[dict | None, WebResult]:
        self.calls += 1
        try:
            result, body = await self._read(request)
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            return None, failed(f"取不到：{type(exc).__name__}", url=request.url)
        if not result.ok:
            return None, result
        import json

        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            return None, failed("对端回的不是 JSON", url=result.url)
        if not isinstance(payload, dict):
            return None, failed("JSON 顶层不是对象", url=result.url)
        return payload, result

    def info(self) -> dict:
        state, detail = self.status()
        return {"state": state, "detail": detail, "calls": self.calls, "last_error": self.last_error}


__all__ = ["WebHTTPAdapter", "web_enabled_from_config", "DEFAULT_USER_AGENT"]
