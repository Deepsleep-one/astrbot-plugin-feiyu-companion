"""适配器层（M3 记忆只读；M5 投递账本落盘；M5.3 真实发送与聊天记录）。"""

from .astrbot_history import AstrBotHistory
from .astrbot_transport import AstrBotTransport, transport_for
from .bili_share_bridge import PluginShareBridge, share_mode_from_config
from .delivery_store import JsonFileDeliveryStore
from .share_inbox import JsonlShareInbox, LedgerCorrupt
from .memory_store import MemoryStoreAdapter

__all__ = [
    "AstrBotHistory",
    "AstrBotTransport",
    "JsonFileDeliveryStore",
    "JsonlShareInbox",
    "LedgerCorrupt",
    "PluginShareBridge",
    "MemoryStoreAdapter",
    "share_mode_from_config",
    "transport_for",
]
