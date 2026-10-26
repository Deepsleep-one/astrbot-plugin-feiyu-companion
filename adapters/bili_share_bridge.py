"""分享交接的适配器（M6-D）：把 B站插件那侧的 `bili_share_port()` 接过来。

三条硬规矩：

1. **只在 `coordinated` 模式下交接**。`legacy` 是「B站自己发」，`shadow` 是
   「只算不发」；这两种情况下这个适配器一律说自己不可用，绝不去抢发送权。
2. **对端缺失就是 `integration_pending`**，不是「没有要发的」——前者说明接线没
   到位，后者说明队列空了，两者在日志里必须能分开。
3. **认领回来的东西先过白名单**（`normalize_ticket`）：状态不是 `claimed`、正文
   为空、缺对账号的，一条都不发。
"""

from __future__ import annotations

from typing import Any

from ..ports.bili_share_outbox import (
    SHARE_BRIDGE_NAME,
    ShareBridgeUnavailable,
    ShareTicket,
    ShareTicketError,
    normalize_ticket,
)

BRIDGE_METHOD = "bili_share_port"
DEFAULT_PROVIDER = "astrbot_plugin_feiyu_bili"
MODE_LEGACY = "legacy"
MODE_SHADOW = "shadow"
MODE_COORDINATED = "coordinated"
SHARE_MODES = (MODE_LEGACY, MODE_SHADOW, MODE_COORDINATED)


def share_mode_from_config(cfg: dict | None) -> str:
    """配置里写错了就退回 shadow：宁可只算不发，也不偷偷换成能发的模式。"""
    raw = str((cfg or {}).get("bili_share_mode") or "").strip().lower()
    return raw if raw in SHARE_MODES else MODE_SHADOW


class PluginShareBridge:
    """对端插件提供的分享交接面。"""

    name = SHARE_BRIDGE_NAME

    def __init__(
        self,
        context: Any,
        *,
        provider_plugin: str = DEFAULT_PROVIDER,
        mode: str = MODE_SHADOW,
        enabled: bool = True,
        logger: Any = None,
    ) -> None:
        self.context = context
        self.provider_plugin = (provider_plugin or DEFAULT_PROVIDER).strip()
        self.mode = mode if mode in SHARE_MODES else MODE_SHADOW
        self.enabled = bool(enabled)
        self.logger = logger
        self.calls = 0
        self.last_error = ""

    # ------------------------------------------------------------------ 内部
    def _log(self, text: str) -> None:
        if self.logger is not None:
            try:
                self.logger.warning(text)
            except Exception:  # pragma: no cover - 日志不该拖垮功能
                pass

    def instance(self):
        if self.context is None:
            return None
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            return None
        try:
            meta = getter(self.provider_plugin)
        except Exception:
            return None
        if meta is None:
            return None
        if getattr(meta, "activated", True) is False:
            return None
        return getattr(meta, "star_cls", None) or getattr(meta, "star_cls_type", None)

    def facade(self):
        star = self.instance()
        if star is None:
            return None
        method = getattr(star, BRIDGE_METHOD, None)
        if not callable(method):
            return None
        try:
            return method()
        except Exception as exc:  # pragma: no cover - 对端自己炸了
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            self._log(f"对端交接面炸了：{self.last_error}")
            return None

    def _off_reason(self) -> str:
        if self.mode != MODE_COORDINATED:
            return (
                f"分享模式是 {self.mode}：M6-D 只在 coordinated 下交接，"
                "这一版不动发送权"
            )
        if not self.enabled:
            return "分享交接关着（bili_bridge_enabled=false）"
        return ""

    # ------------------------------------------------------------------ 状态
    def bridge_state(self) -> tuple[str, str]:
        """返回 (状态, 说明)。状态只可能是 read_only / integration_pending。"""
        reason = self._off_reason()
        if reason:
            return "integration_pending", reason
        if self.instance() is None:
            return "integration_pending", f"没找到已加载的 {self.provider_plugin}（未安装或未激活）"
        facade = self.facade()
        if facade is None:
            return "integration_pending", "对端还没有提供 bili_share_port() 交接面"
        status = getattr(facade, "status", None)
        if callable(status):
            try:
                info = status() or {}
                counts = info.get("counts") or {}
                return "read_only", f"交接面就绪（outbox {counts.get('total', 0)} 条）"
            except Exception:
                pass
        return "read_only", "交接面就绪"

    # ------------------------------------------------------------------ 交接
    def claim(self, *, scope: str = "", limit: int = 1) -> list[ShareTicket]:
        reason = self._off_reason()
        if reason:
            raise ShareBridgeUnavailable(reason)
        facade = self.facade()
        if facade is None:
            raise ShareBridgeUnavailable(
                "integration_pending：对端没提供 bili_share_port()，无法交接"
            )
        try:
            rows = facade.claim({"scope": scope, "limit": max(1, int(limit)), "token": ""})
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            raise ShareBridgeUnavailable(f"认领失败：{self.last_error}") from exc
        self.calls += 1
        tickets: list[ShareTicket] = []
        for raw in rows or []:
            try:
                tickets.append(normalize_ticket(raw))
            except ShareTicketError as exc:
                # 一条坏票据不该让整批作废；但也绝不发它。
                self.last_error = str(exc)[:200]
                self._log(f"跳过一条不合规票据：{self.last_error}")
        return tickets

    def ack(self, ticket: ShareTicket, outcome: str, detail: str = "") -> dict:
        facade = self.facade()
        if facade is None:
            raise ShareBridgeUnavailable("对端交接面没了，回执写不回去")
        try:
            return facade.ack(
                {"generation": ticket.generation, "outcome": outcome, "detail": detail}
            )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            raise ShareBridgeUnavailable(f"回执失败：{self.last_error}") from exc
