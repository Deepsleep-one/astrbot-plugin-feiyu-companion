"""B站只读桥的适配器（M6-A）。

职责只有一个：**从已加载的 B站插件实例上取到 `bili_read_port()`，把查询递过去，
再把回来的 dict 交给 `ports.bili.normalize_snapshot()` 做白名单归一。**

这里刻意守住的几条：

- 不 `import` B站插件的任何模块，不碰它那边的库文件，不实例化任何阅读存储对象；
- 找不到插件、插件没激活、插件还没提供 facade → `BiliUnavailable`（外面显示 integration_pending），
  **不抛别的异常去打断聊天**；
- `bili_bridge_mode` 只认真实现了的 `read_only`；`shadow` / `coordinated` 要等 M6-C 的
  generation/ACK 握手，现在直接拒绝，绝不允许「配了 shadow 就偷偷真发」；
- 对端如果返回 awaitable（异步 facade），也拒绝——只读查询必须是同步的，
  要异步就另立一个端口版本，不在这里模糊掉。
"""

from __future__ import annotations

import inspect
from typing import Any

from ..ports.bili import (
    BiliContractError,
    BiliQuery,
    BiliSnapshot,
    BiliUnavailable,
    PORT_NAME,
    empty_snapshot,
    normalize_snapshot,
)

BRIDGE_METHOD = "bili_read_port"
DEFAULT_PROVIDER = "astrbot_plugin_feiyu_bili"
MODES = ("read_only", "shadow", "coordinated")


def bili_mode_from_config(config: dict | None) -> str:
    """读 `bili_bridge_mode`；不认识的值按 `read_only` 处理（宁可少做，不许多做）。"""
    raw = str((config or {}).get("bili_bridge_mode") or "read_only").strip().lower()
    return raw if raw in MODES else "read_only"


def bili_enabled_from_config(config: dict | None) -> bool:
    return bool((config or {}).get("bili_bridge_enabled", False))


class PluginBiliReadAdapter:
    """通过宿主插件注册表找到 B站实例，取它的只读 facade。"""

    def __init__(
        self,
        context: Any,
        *,
        enabled: bool = True,
        provider_plugin: str = DEFAULT_PROVIDER,
        mode: str = "read_only",
        logger: Any = None,
    ) -> None:
        self.context = context
        self.enabled = bool(enabled)
        self.provider_plugin = str(provider_plugin or DEFAULT_PROVIDER)
        self.mode = mode if mode in MODES else "read_only"
        self.logger = logger
        self._last_error = ""
        self._calls = 0

    @property
    def calls(self) -> int:
        """往对端打了几次（诊断与测试要看这个数是不是 0）。"""
        return self._calls

    def _off_reason(self) -> str:
        return "桥在配置里关着（bili_bridge_enabled=false）：本轮只做契约，不去问对端"

    # ---- 发现 ------------------------------------------------------------
    def instance(self) -> Any | None:
        """取到已加载的 B站插件实例；取不到就返回 None（不抛）。"""
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            return None
        try:
            meta = getter(self.provider_plugin)
        except Exception:  # noqa: BLE001 - 宿主接口异常不该影响聊天
            return None
        if meta is None:
            return None
        if getattr(meta, "activated", True) is False:
            return None
        return getattr(meta, "star_cls", None)

    def facade(self) -> Any | None:
        inst = self.instance()
        if inst is None:
            return None
        method = getattr(inst, BRIDGE_METHOD, None)
        if not callable(method):
            return None
        try:
            return method()
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}"
            return None

    def bridge_state(self) -> tuple[str, str]:
        """(状态, 说明)：给状态页用，不抛异常。"""
        if not self.enabled:
            return ("integration_pending", self._off_reason())
        if self.mode != "read_only":
            return (
                "integration_pending",
                f"{self.mode} 模式要等 M6-C 的 generation/ACK 握手，本版只做 read_only",
            )
        if self.instance() is None:
            return ("integration_pending", f"没找到已加载的 {self.provider_plugin}（未安装或未激活）")
        if self.facade() is None:
            return (
                "integration_pending",
                f"{self.provider_plugin} 还没有提供 {PORT_NAME} 只读 facade（{BRIDGE_METHOD}()）",
            )
        return ("read_only", f"已连上 {self.provider_plugin} 的 {PORT_NAME}")

    # ---- 查询 ------------------------------------------------------------
    def snapshot(self, query: BiliQuery) -> BiliSnapshot:
        if not isinstance(query, BiliQuery):
            raise BiliContractError("只读查询必须是 BiliQuery（宿主构造，不让模型自己拼）")
        if not self.enabled:
            raise BiliUnavailable(f"integration_pending：{self._off_reason()}")
        if self.mode != "read_only":
            raise BiliUnavailable(
                f"integration_pending：{self.mode} 模式尚未实现（要等 M6-C 的握手），本版只做 read_only"
            )
        facade = self.facade()
        if facade is None:
            state, detail = self.bridge_state()
            raise BiliUnavailable(f"integration_pending：{detail}")
        call = getattr(facade, "snapshot", None)
        if not callable(call):
            raise BiliUnavailable(
                f"integration_pending：{PORT_NAME} 的 facade 上没有 snapshot()"
            )
        result = call(query.to_dict())
        self._calls += 1
        if inspect.isawaitable(result):
            raise BiliContractError("只读 facade 必须同步返回：异步接口请另立端口版本")
        return normalize_snapshot(result, query)

    def snapshot_or_empty(self, query: BiliQuery) -> BiliSnapshot:
        """诊断用：桥不可用时返回带原因的**空**快照，而不是抛异常。"""
        try:
            return self.snapshot(query)
        except BiliUnavailable as exc:
            return empty_snapshot(str(exc))
        except BiliContractError as exc:
            return empty_snapshot(f"契约不符：{exc}")

    def last_error(self) -> str:
        return self._last_error
