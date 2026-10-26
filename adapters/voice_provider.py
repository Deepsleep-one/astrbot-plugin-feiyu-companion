"""把 `feiyu_voice` 插件暴露的 TTS 提供商接成陪伴端的语音端口。

为什么是「借」而不是「自己再实现一遍」：
- 声卡那边的令牌、本机套接字、音频校验、失败关闭（`VOICE_PERMISSION`）都已经写好了；
  这边再抄一份就等于复制凭证读取与网络代码，边界会立刻变糊。
- 所以这里只做三件事：找到那个 provider、问它能不能用、把一段中文交给它。

它自己**不发消息**：合成出来的 wav 路径交给投递链路，走哪条会话由 `Delivery` 决定。
"""

from __future__ import annotations

from typing import Any

from ..ports.voice import (
    DEFAULT_MAX_CHARS,
    DEFAULT_PROVIDER_ID,
    VOICE_PORT_NAME,
    VOICE_VERSION,
    VoiceRequest,
    VoiceResult,
    failed,
    prepare,
)

#: 声卡插件在 `register_provider_adapter` 里用的 id；对不上就当作没装。
VOICE_PROVIDER_METHOD = "synthesize"


def voice_enabled_from_config(cfg: dict) -> bool:
    """总开关。缺省关，写错也只有真值才算开。"""
    value = (cfg or {}).get("voice_enable", False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def voice_provider_from_config(cfg: dict) -> str:
    text = str((cfg or {}).get("voice_provider_id", "") or "").strip()
    return text or DEFAULT_PROVIDER_ID


class PluginVoiceAdapter:
    """实现 `ports.voice.VoicePort`，底下是另一个插件的 provider 对象。"""

    def __init__(
        self,
        context: Any,
        *,
        provider_id: str = DEFAULT_PROVIDER_ID,
        enabled: bool = True,
        logger: Any = None,
    ) -> None:
        self.context = context
        self.provider_id = str(provider_id or DEFAULT_PROVIDER_ID)
        self.enabled = bool(enabled)
        self.logger = logger
        self.calls = 0
        self.last_error = ""

    # ---- 找对象 -------------------------------------------------------
    def provider(self) -> Any | None:
        if self.context is None:
            return None
        try:
            return self.context.get_provider_by_id(self.provider_id)
        except Exception as exc:  # pragma: no cover - 宿主异常不该炸掉整条链路
            self.last_error = type(exc).__name__
            return None

    def _off_reason(self) -> str:
        if not self.enabled:
            return "语音总开关关着（voice_enable=false）：默认只发文字"
        if self.provider() is None:
            return (
                f"没找到语音提供商 {self.provider_id}"
                "（feiyu_voice 插件没装/没开，或 id 改了）：这一条按文字处理"
            )
        return ""

    # ---- 端口实现 -----------------------------------------------------
    def status(self) -> tuple[str, str]:
        """返回 (状态, 说明)。状态只会是 integration_pending 或 ready。"""
        reason = self._off_reason()
        if reason:
            return "integration_pending", reason
        provider = self.provider()
        if not hasattr(provider, VOICE_PROVIDER_METHOD):
            return (
                "integration_pending",
                f"提供商 {self.provider_id} 还是老版本，没有 "
                f"{VOICE_PROVIDER_METHOD}() 这个对外入口：这一条按文字处理",
            )
        return "ready", f"语音可用：{self.provider_id}（只念中文，失败就退回文字）"

    async def synthesize(self, request: VoiceRequest) -> VoiceResult:
        state, detail = self.status()
        if state != "ready":
            return failed(detail, provider=self.provider_id)
        body, why = prepare(getattr(request, "text", ""), max_chars=getattr(request, "max_chars", DEFAULT_MAX_CHARS))
        if not body:
            return failed(why, provider=self.provider_id)
        provider = self.provider()
        self.calls += 1
        try:
            raw = provider.synthesize(body)
            if hasattr(raw, "__await__"):
                raw = await raw
        except Exception as exc:
            self.last_error = type(exc).__name__
            if self.logger is not None:
                self.logger.info("[feiyu_companion] 语音合成没成（%s）", type(exc).__name__)
            return failed(f"声卡报错：{type(exc).__name__}", provider=self.provider_id)
        path = str(raw or "")
        if not path:
            self.last_error = "empty"
            return failed("声卡没给出音频（保留文字）", provider=self.provider_id)
        return VoiceResult(ok=True, audio_path=path, provider=self.provider_id)

    def info(self) -> dict[str, Any]:
        state, detail = self.status()
        return {
            "port": VOICE_PORT_NAME,
            "version": VOICE_VERSION,
            "provider_id": self.provider_id,
            "enabled": self.enabled,
            "state": state,
            "detail": detail,
            "calls": self.calls,
            "last_error": self.last_error,
        }


__all__ = [
    "VOICE_PROVIDER_METHOD",
    "PluginVoiceAdapter",
    "voice_enabled_from_config",
    "voice_provider_from_config",
]
