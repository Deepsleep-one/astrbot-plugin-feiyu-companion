"""M9-A：识屏（一次性屏幕快照）的契约。

这一版只做「一次快照、看完就过期」，不做持续观察——持续采集按 §6.5 要单独授权、
有可见状态与到期自动停止，那是另一道门。

硬规矩（全部写进测试）：
- 只认显式请求：`grant_once(CAP_SCREEN)` 是单轮的，不延续到下一句。
- 只能看一次：`ScreenRequest.target` 必须写清是「整屏」还是某个窗口，
  回来的快照必须**还是那个目标**，对不上就拒（不是「差不多就行」）。
- 有效期很短：快照自己带 `expires_at`，过期就作废，不拿旧帧冒充现场。
- 遮罩与脱敏：文字先过 `redact_screen_text`，窗口标题命中敏感词直接不看。
- 只读，不隐含控制：这个模块里没有任何键鼠/输入注入的东西（静态检查盯着）。
- 云端读不到本机时明确不可用，绝不伪造「我看了一眼」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

SCREEN_PORT_NAME = "ScreenPortV1"
SCREEN_VERSION = 1
DEFAULT_MAX_CHARS = 1200
DEFAULT_TTL_SECONDS = 60
MAX_TTL_SECONDS = 300
TARGET_SCREEN = "screen"
TARGET_WINDOW = "window"
TARGETS = (TARGET_SCREEN, TARGET_WINDOW)
SOURCE_SCREEN = "screen"
SOURCE_CAMERA = "camera"
SOURCES = (SOURCE_SCREEN, SOURCE_CAMERA)
DEFAULT_DEVICE = "primary"

# 窗口标题里出现这些词就不看：宁可少看一次，不要把她眼睛放到支付页面上
SENSITIVE_WORDS = (
    "银行", "支付", "支付宝", "微信支付", "钱包", "余额", "转账", "验证码",
    "密码", "私钥", "密钥", "身份证", "银行卡", "工资", "社保", "医保",
    "password", "passwd", "1password", "keychain", "bank", "wallet",
)

_TOKEN_RE = re.compile(
    r"(?i)\b(SESSDATA|bili_jct|csrf|token|api[_-]?key|secret|password|passwd|cookie)\b\s*[=:]\s*\S+"
)
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_CARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_URL_RE = re.compile(r"https?://\S+")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")


class ScreenError(ValueError):
    """这个识屏请求本身不成立。"""


class ScreenUnavailable(RuntimeError):
    """没有采集端 / 采集失败：明确不可用，不编造。"""


class ScreenMismatch(ScreenError):
    """回来的不是刚才要看的那块屏幕。"""


class ScreenExpired(ScreenError):
    """这张快照过期了，不拿旧的冒充现场。"""


class ScreenSensitive(ScreenError):
    """这块屏幕上像是敏感内容，不看。"""


@dataclass(frozen=True)
class ScreenRequest:
    target: str = TARGET_SCREEN
    scene: str = "private"
    device: str = DEFAULT_DEVICE
    window: str = ""
    max_chars: int = DEFAULT_MAX_CHARS
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    source: str = SOURCE_SCREEN

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ScreenError(f"不认识的采集源：{self.source}（只有 screen / camera）")
        if self.target not in TARGETS:
            raise ScreenError(f"不认识的识屏目标：{self.target}（只有 screen / window）")
        if self.source == SOURCE_CAMERA and self.target != TARGET_SCREEN:
            raise ScreenError("摄像头没有「窗口」：要窗户就去看屏幕")
        if self.target == TARGET_WINDOW and not str(self.window or "").strip():
            raise ScreenError("要看哪个窗口？这一句没说清")
        # 0 秒是「明确要求一张立刻作废的图」，不是「没填」，不能被兜底成默认值
        ttl = DEFAULT_TTL_SECONDS if self.ttl_seconds is None else int(self.ttl_seconds)
        if not 1 <= ttl <= MAX_TTL_SECONDS:
            raise ScreenError(f"有效期要在 1–{MAX_TTL_SECONDS} 秒之间：{ttl}")

    def to_dict(self) -> dict:
        return {
            "port": SCREEN_PORT_NAME,
            "version": SCREEN_VERSION,
            "source": self.source,
            "target": self.target,
            "device": self.device,
            "window": self.window,
            "max_chars": int(self.max_chars),
            "ttl_seconds": int(self.ttl_seconds),
        }


@dataclass(frozen=True)
class ScreenFrame:
    """采集端交回来的一帧。`ok=False` 时 `text` 一定为空。"""

    ok: bool
    reason: str = ""
    target: str = TARGET_SCREEN
    source: str = SOURCE_SCREEN
    device: str = DEFAULT_DEVICE
    window: str = ""
    captured_at: str = ""
    expires_at: str = ""
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    width: int = 0
    height: int = 0
    text: str = ""
    boxes: tuple = ()
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "reason": self.reason,
            "target": self.target,
            "source": self.source,
            "device": self.device,
            "window": self.window,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
            "ttl_seconds": int(self.ttl_seconds),
            "width": int(self.width),
            "height": int(self.height),
            "text": self.text,
            "boxes": list(self.boxes),
        }


@dataclass(frozen=True)
class ScreenResult:
    ok: bool
    reason: str = ""
    text: str = ""
    device: str = ""
    window: str = ""
    captured_at: str = ""
    expires_at: str = ""
    redactions: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "reason": self.reason,
            "text": self.text,
            "device": self.device,
            "window": self.window,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
            "redactions": int(self.redactions),
        }


def failed(reason: str, *, device: str = "", window: str = "") -> ScreenResult:
    return ScreenResult(ok=False, reason=reason, device=device, window=window)


def redact_screen_text(text: str, *, limit: int = DEFAULT_MAX_CHARS) -> tuple[str, int]:
    """把屏幕上不该外传的东西抹掉，返回 (正文, 抹掉几处)。"""
    body = str(text or "")
    hits = 0
    for pattern in (_TOKEN_RE, _BEARER_RE, _CARD_RE, _ID_RE, _PHONE_RE, _URL_RE, _HEX_RE):
        body, count = pattern.subn("〔已隐去〕", body)
        hits += count
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    cap = max(200, int(limit))
    if len(body) > cap:
        body = body[:cap].rstrip() + "…"
    return body, hits


def looks_sensitive(*, window: str = "", text: str = "") -> bool:
    """窗口标题命中敏感词 → 不看；正文命中 → 只是提醒（正文已经脱敏，不改判定）。"""
    window_hay = str(window or "").lower()
    if any(word.lower() in window_hay for word in SENSITIVE_WORDS):
        return True
    text_hay = str(text or "").lower()
    return any(word.lower() in text_hay for word in SENSITIVE_WORDS)


def target_of(*, window: str = "") -> str:
    return TARGET_WINDOW if str(window or "").strip() else TARGET_SCREEN


def _parse(moment: str) -> datetime | None:
    raw = str(moment or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _source_label(source: str) -> str:
    return "摄像头" if source == SOURCE_CAMERA else "屏幕"


def validate_frame(frame, request: ScreenRequest, *, now: datetime) -> ScreenFrame:
    """验收门：目标对得上、没过期、标题不敏感，才允许把内容交出去。"""
    if not isinstance(frame, ScreenFrame):
        raise ScreenError("采集端返回的不是 ScreenFrame")
    if not frame.ok:
        return frame  # 采集失败原样交回，由调用方如实说「没看成」
    if frame.source and frame.source != request.source:
        raise ScreenMismatch(
            f"要看的是{_source_label(request.source)}，回来的是{_source_label(frame.source)}：对不上就不看"
        )
    if frame.target != request.target:
        raise ScreenMismatch(
            f"要看的是 {request.target}，回来的是 {frame.target}：对不上就不看"
        )
    if request.target == TARGET_WINDOW and frame.window.strip() != request.window.strip():
        raise ScreenMismatch(
            f"要看的是「{request.window}」，回来的是「{frame.window}」：对不上就不看"
        )
    expires = _parse(frame.expires_at)
    if expires is not None:
        if expires.tzinfo is None and now.tzinfo is not None:
            expires = expires.replace(tzinfo=now.tzinfo)
        if now > expires:
            raise ScreenExpired("这张快照已经过期了，不拿旧的冒充现在的屏幕")
    if looks_sensitive(window=frame.window):
        raise ScreenSensitive(
            f"「{frame.window}」看着像敏感窗口（支付/密码/证件），这一眼不看"
        )
    text, hits = redact_screen_text(frame.text, limit=request.max_chars)
    return ScreenFrame(
        ok=True,
        target=frame.target,
        source=frame.source,
        device=frame.device,
        window=frame.window,
        captured_at=frame.captured_at,
        expires_at=frame.expires_at,
        ttl_seconds=frame.ttl_seconds,
        width=frame.width,
        height=frame.height,
        text=text,
        boxes=tuple(frame.boxes),
        extra={**dict(frame.extra), "redactions": hits},
    )


__all__ = [
    "SCREEN_PORT_NAME",
    "SCREEN_VERSION",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_TTL_SECONDS",
    "MAX_TTL_SECONDS",
    "TARGET_SCREEN",
    "TARGET_WINDOW",
    "TARGETS",
    "SOURCE_SCREEN",
    "SOURCE_CAMERA",
    "SOURCES",
    "DEFAULT_DEVICE",
    "SENSITIVE_WORDS",
    "ScreenError",
    "ScreenUnavailable",
    "ScreenMismatch",
    "ScreenExpired",
    "ScreenSensitive",
    "ScreenRequest",
    "ScreenFrame",
    "ScreenResult",
    "failed",
    "redact_screen_text",
    "looks_sensitive",
    "target_of",
    "validate_frame",
]
