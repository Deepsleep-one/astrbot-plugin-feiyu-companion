"""语音合成的只读端口：谁也不能凭这个端口改任何东西，只能「把一句话变成一段音频」。

M7-A 的边界（写死在这里，别在下游再放宽）：
- 默认不发语音；只有「主人这一次明确要」才有单轮授权（授权不跨轮，见 domain/grants.py）。
- 段子必须先过 `prepare()` 的中文与长度体检，不合格就诚实退回文字，不硬念。
- 声卡不可用（没装/没开/没 token/socket 不在/服务没 ready）一律 `integration_pending`，
  不许拿文字冒充语音，也不许静音假成功。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

VOICE_PORT_NAME = "VoicePortV1"
VOICE_VERSION = 1

DEFAULT_MAX_CHARS = 120
DEFAULT_PROVIDER_ID = "feiyu/gpt-sovits"

#: 与声卡侧 `prepare_text` 同源的体检规则：先在这里拒，省得白调一次 socket。
_CODE_FENCE = re.compile(r"```[\s\S]*?```")
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_LATIN = re.compile(r"[A-Za-z]")
_MARKUP = (
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.M), ""),
    (re.compile(r"（[^）]*）|\([^)]*\)"), ""),
    (re.compile(r"\[[^\]]*\]"), ""),
)


class VoiceUnavailable(RuntimeError):
    """声卡这条路现在走不通。调用方应当退回文字，而不是重试到成功。"""


class VoiceContractError(ValueError):
    """调用方给的东西不符合端口约定。"""


def prepare(text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str, str]:
    """把一段话整理成可以念的中文；不能念时返回 `("", 原因)`。

    规则偏保守：含代码围栏、链接、任何拉丁字母都直接放弃；这是为了让「念出来的」
    与「写出来的」不是两套东西，也避免把英文单词念成奇怪的中文音。
    """
    raw = str(text or "").strip()
    if not raw:
        return "", "没有可念的内容"
    if _CODE_FENCE.search(raw):
        return "", "这段里有代码，念出来没有意义"
    body = _CODE_FENCE.sub("", raw)
    body = _URL.sub("", body)
    if _URL.search(raw):
        return "", "这段里有链接，念出来没有意义"
    for pattern, repl in _MARKUP:
        body = pattern.sub(repl, body)
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return "", "去掉格式以后没有剩下可念的内容"
    if _LATIN.search(body):
        return "", "这段里有英文或代码标识，声卡只念中文"
    limit = max(1, int(max_chars or DEFAULT_MAX_CHARS))
    if len(body) > limit:
        return "", f"太长（{len(body)} 字，上限 {limit}）"
    return body, ""


@dataclass(frozen=True)
class VoiceRequest:
    """一次合成的请求。只有正文、场景与上限，不带任何凭证。"""

    text: str
    scene: str = "private"
    max_chars: int = DEFAULT_MAX_CHARS
    provider_id: str = DEFAULT_PROVIDER_ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": VOICE_PORT_NAME,
            "version": VOICE_VERSION,
            "text": self.text,
            "scene": self.scene,
            "max_chars": self.max_chars,
            "provider_id": self.provider_id,
        }


@dataclass(frozen=True)
class VoiceResult:
    """合成结果。`ok=False` 时 `audio_path` 一定是空串（不造假成功）。"""

    ok: bool
    audio_path: str = ""
    duration_ms: int = 0
    reason: str = ""
    provider: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": VOICE_PORT_NAME,
            "version": VOICE_VERSION,
            "ok": self.ok,
            "audio_path": self.audio_path,
            "duration_ms": self.duration_ms,
            "reason": self.reason,
            "provider": self.provider,
            "extra": dict(self.extra),
        }


def failed(reason: str, *, provider: str = "") -> VoiceResult:
    return VoiceResult(ok=False, reason=str(reason or "声卡没说原因"), provider=provider)


class VoicePort(Protocol):
    """语音端口：状态 + 合成。故意没有「发送」，发送走投递链路。"""

    def status(self) -> tuple[str, str]:
        ...  # pragma: no cover - 协议

    async def synthesize(self, request: VoiceRequest) -> VoiceResult:
        ...  # pragma: no cover - 协议


__all__ = [
    "VOICE_PORT_NAME",
    "VOICE_VERSION",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_PROVIDER_ID",
    "VoiceRequest",
    "VoiceResult",
    "VoiceUnavailable",
    "VoiceContractError",
    "VoicePort",
    "prepare",
    "failed",
]
