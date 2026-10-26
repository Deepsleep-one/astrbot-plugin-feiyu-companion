"""M8-A：有界只读网页端口（WebPortV1）。

三条底线写在这个文件里，而不是散在调用处：

1. **只读**：只有 GET，没有 POST/PUT/上传/登录；不读 Cookie、不带任何凭证。
2. **不碰私网**：回环、私有网段、链路本地、云元数据地址一律拒；
   重定向要逐跳复核，不能靠"第一跳合法"混过去（实施方案 §4.10）。
3. **外部内容是数据**：抓回来的文字只是材料，不构成指令，也不换任何授权。

DNS 重绑定这类问题这里不假装解决——缓解手段是"只抓主人当次明确给的目标"
和"逐跳校验"，不是靠一张黑名单。
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from typing import Protocol

WEB_PORT_NAME = "WebPortV1"
WEB_VERSION = 1

DEFAULT_MAX_BYTES = 400_000
DEFAULT_MAX_CHARS = 1200
DEFAULT_TIMEOUT = 15.0
MAX_REDIRECTS = 3

ALLOWED_SCHEMES = ("http", "https")

#: 私网 / 回环 / 元数据等不该被"网页读取"够到的网段
_BLOCKED_V4 = (
    "127.", "0.", "10.", "192.168.", "169.254.",
    "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.",
    "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.",
    "172.28.", "172.29.", "172.30.", "172.31.",
)
_BLOCKED_NAMES = ("localhost", "localhost.localdomain", "metadata", "metadata.google.internal")

_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://")
_HOST_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://(?:[^/@]*@)?\[?([^/:\]?#]+)\]?")


class WebContractError(ValueError):
    """调用方给的目标不合规（就地拒，不去网络）。"""


class WebUnavailable(RuntimeError):
    """端口这次拿不到东西（关着 / 没通道 / 对端失败）。"""


def _host_of(url: str) -> str:
    match = _HOST_RE.match((url or "").strip())
    return (match.group(1) if match else "").strip().lower().rstrip(".")


def is_private_host(host: str) -> bool:
    """回环、私网、链路本地、元数据别名 → True。"""
    name = (host or "").strip().lower().strip("[]").rstrip(".")
    if not name:
        return True
    if name in _BLOCKED_NAMES:
        return True
    if name.startswith(_BLOCKED_V4):
        return True
    if name in ("::1", "::", "0:0:0:0:0:0:0:1"):
        return True
    if name.startswith(("fc", "fd", "fe80", "fe80:")):
        return True
    return False


def parse_target(url: str) -> tuple[str, str]:
    """校验并返回 (规范化 url, host)。不合规抛 WebContractError。"""
    raw = (url or "").strip()
    if not raw:
        raise WebContractError("没给目标地址")
    match = _SCHEME_RE.match(raw)
    if not match:
        raise WebContractError("地址要以 http:// 或 https:// 开头")
    scheme = match.group(1).lower()
    if scheme not in ALLOWED_SCHEMES:
        raise WebContractError(f"只读 http/https，不接受 {scheme}://")
    rest = raw[match.end():]
    if "@" in rest.split("/", 1)[0]:
        raise WebContractError("地址里带了账号密码：这种链接一律不看")
    host = _host_of(raw)
    if not host:
        raise WebContractError("地址里没有主机名")
    if is_private_host(host):
        raise WebContractError(f"{host} 是本机或私网地址：网页读取不碰这些")
    return raw, host


@dataclass(frozen=True)
class WebRequest:
    url: str
    scene: str = "private"
    max_chars: int = DEFAULT_MAX_CHARS
    max_bytes: int = DEFAULT_MAX_BYTES
    timeout: float = DEFAULT_TIMEOUT

    def to_dict(self) -> dict:
        return {
            "port": WEB_PORT_NAME,
            "version": WEB_VERSION,
            "url": self.url,
            "scene": self.scene,
            "max_chars": int(self.max_chars),
            "max_bytes": int(self.max_bytes),
            "timeout": float(self.timeout),
        }


@dataclass(frozen=True)
class WebResult:
    ok: bool
    url: str = ""
    title: str = ""
    text: str = ""
    reason: str = ""
    bytes_read: int = 0
    elapsed_ms: int = 0
    redirects: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "reason": self.reason,
            "bytes_read": int(self.bytes_read),
            "elapsed_ms": int(self.elapsed_ms),
            "redirects": list(self.redirects),
            "extra": dict(self.extra),
        }


def failed(reason: str, *, url: str = "", elapsed_ms: int = 0) -> WebResult:
    return WebResult(ok=False, url=url, reason=str(reason)[:200], elapsed_ms=int(elapsed_ms))


_SCRIPT_RE = re.compile(r"<(script|style|noscript|template)\b[^>]*>.*?</\1>", re.I | re.S)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BLOCK_TAG_RE = re.compile(r"</?(p|div|br|li|tr|h[1-6]|section|article|header|footer|blockquote)\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u3000\xa0\u200b]+")
_NL_RE = re.compile(r"\n{3,}")


def html_title(raw: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", raw or "", re.I | re.S)
    if not match:
        return ""
    return _html.unescape(_TAG_RE.sub("", match.group(1))).strip()[:120]


def html_to_text(raw: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """把 HTML 变成能读的纯文本；不保留标签、不要脚本样式。"""
    text = _SCRIPT_RE.sub(" ", raw or "")
    text = _COMMENT_RE.sub(" ", text)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    text = _NL_RE.sub("\n\n", text).strip()
    limit = max(40, int(max_chars))
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


class WebPort(Protocol):
    def status(self) -> tuple[str, str]:
        ...

    async def fetch(self, request: WebRequest) -> WebResult:
        ...

    async def fetch_json(self, request: WebRequest) -> tuple[dict | None, WebResult]:
        ...


__all__ = [
    "WEB_PORT_NAME",
    "WEB_VERSION",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_TIMEOUT",
    "MAX_REDIRECTS",
    "ALLOWED_SCHEMES",
    "WebContractError",
    "WebUnavailable",
    "WebRequest",
    "WebResult",
    "WebPort",
    "failed",
    "is_private_host",
    "parse_target",
    "html_title",
    "html_to_text",
]
