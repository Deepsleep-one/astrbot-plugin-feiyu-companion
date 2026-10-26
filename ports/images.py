"""出图的只读端口：把一句描述变成一张本机生成的图。

M7-B 的边界（写死在这里）：
- 默认不出图；只有「主人这一次明确要」才换一次单轮授权（`CAP_IMAGE`）。
- 生成器在本机（ComfyUI），bot 在云端；**能不能连上由部署决定**，连不上就如实说
  `integration_pending`，绝不拿旧图、占位图或网图冒充。
- 这里只有 prompt 与出图参数，没有任何凭证、没有任意 URL：base_url 由部署方给定，
  默认只认本机回环地址，免得「一句描述」被顺手发到别人的服务器上。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

IMAGE_PORT_NAME = "ImagePortV1"
IMAGE_VERSION = 1

DEFAULT_MAX_CHARS = 300
DEFAULT_SIZE = "1024x1024"
DEFAULT_STEPS = 8
DEFAULT_TIMEOUT = 120.0
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

_CODE_FENCE = re.compile(r"```[\s\S]*?```")
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)


class ImageUnavailable(RuntimeError):
    """出图这条路现在走不通（没连上/没开/超时）。调用方应退回文字，不要重试到成功。"""


class ImageContractError(ValueError):
    """调用方给的东西不符合端口约定。"""


def prepare_prompt(text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str, str]:
    """整理描述。含代码块或链接的直接拒：那不是我该画的东西，也避免顺手抓网页。"""
    raw = str(text or "").strip()
    if not raw:
        return "", "没有描述，不知道画什么"
    if _CODE_FENCE.search(raw):
        return "", "这段里有代码，不是画画的描述"
    if _URL.search(raw):
        return "", "这段里有链接：不去抓网页，也不画网页"
    body = re.sub(r"\s+", " ", raw).strip()
    if not body:
        return "", "去掉空白以后没有剩下描述"
    limit = max(1, int(max_chars or DEFAULT_MAX_CHARS))
    if len(body) > limit:
        return "", f"描述太长（{len(body)} 字，上限 {limit}）"
    return body, ""


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    scene: str = "private"
    size: str = DEFAULT_SIZE
    steps: int = DEFAULT_STEPS
    seed: int = 0
    max_chars: int = DEFAULT_MAX_CHARS
    timeout: float = DEFAULT_TIMEOUT

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": IMAGE_PORT_NAME,
            "version": IMAGE_VERSION,
            "prompt": self.prompt,
            "scene": self.scene,
            "size": self.size,
            "steps": self.steps,
            "seed": self.seed,
            "max_chars": self.max_chars,
            "timeout": self.timeout,
        }


@dataclass(frozen=True)
class ImageResult:
    """出图结果。`ok=False` 时 `path` 一定是空串（不造假成功）。"""

    ok: bool
    path: str = ""
    reason: str = ""
    elapsed_ms: int = 0
    prompt_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": IMAGE_PORT_NAME,
            "version": IMAGE_VERSION,
            "ok": self.ok,
            "path": self.path,
            "reason": self.reason,
            "elapsed_ms": self.elapsed_ms,
            "prompt_id": self.prompt_id,
            "extra": dict(self.extra),
        }


def failed(reason: str) -> ImageResult:
    return ImageResult(ok=False, reason=str(reason or "出图没成，但没说原因"))


def _host_of(base_url: str) -> str:
    text = str(base_url or "").strip().lower()
    if not text.startswith(("http://", "https://")):
        return ""
    return text.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]


def is_loopback(base_url: str) -> bool:
    """只允许本机地址：`http://127.0.0.1:8188` 这类。"""
    return _host_of(base_url) in LOOPBACK_HOSTS


def is_private_address(base_url: str) -> bool:
    """RFC1918 私网地址：`http://172.17.0.1:18188` 这类（同一台机器上的 docker 网关）。

    只认 10./172.16-31./192.168. 三段，外加 IPv6 的 fc00::/7 与 link-local。
    公网地址一律不算——描述不该被发到别人的服务器上。
    """
    host = _host_of(base_url)
    if not host:
        return False
    if host in LOOPBACK_HOSTS:
        return True
    if host.startswith(("10.", "192.168.")):
        return True
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return True
    if host.startswith("fc") or host.startswith("fd") or host.startswith("fe80:"):
        return True
    return False


def is_allowed_target(base_url: str, *, allow_lan: bool = False) -> bool:
    """出图目标是否允许：默认只准回环；显式打开 allow_lan 才放行私网（但绝不放行公网）。"""
    if is_loopback(base_url):
        return True
    if allow_lan and is_private_address(base_url):
        return True
    return False


class ImagePort(Protocol):
    def status(self) -> tuple[str, str]:
        ...  # pragma: no cover - 协议

    async def generate(self, request: ImageRequest) -> ImageResult:
        ...  # pragma: no cover - 协议


__all__ = [
    "IMAGE_PORT_NAME",
    "IMAGE_VERSION",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_SIZE",
    "DEFAULT_STEPS",
    "DEFAULT_TIMEOUT",
    "LOOPBACK_HOSTS",
    "ImageUnavailable",
    "ImageContractError",
    "ImageRequest",
    "ImageResult",
    "ImagePort",
    "prepare_prompt",
    "failed",
    "is_loopback",
    "is_private_address",
    "is_allowed_target",
]
