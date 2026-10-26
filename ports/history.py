"""历史上下文端口。

约束（写死在这里，测试会盯着）：

- 历史**只是事实参考**，永远排在提示词的「数据区」，不进系统提示词；
- 读不到就返回空，绝不编造「最近聊过什么」；
- 本端口只读，不做任何写入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

ROLE_OWNER = "owner"
ROLE_ASSISTANT = "assistant"
ROLE_OTHER = "other"

SOURCE_CONVERSATION = "conversation_history"
SOURCE_PLATFORM = "platform_message_history"
SOURCE_HYBRID = "hybrid"
SOURCE_MODES = (SOURCE_CONVERSATION, SOURCE_PLATFORM, SOURCE_HYBRID)


@dataclass(frozen=True)
class HistoryLine:
    """一条历史发言。"""

    role: str
    text: str
    at: str = ""

    @property
    def is_owner(self) -> bool:
        return self.role == ROLE_OWNER

    @property
    def is_assistant(self) -> bool:
        return self.role == ROLE_ASSISTANT


@dataclass(frozen=True)
class HistorySlice:
    """一段历史，以及它是从哪儿来的。"""

    lines: tuple[HistoryLine, ...] = ()
    source: str = SOURCE_CONVERSATION
    note: str = ""

    def __bool__(self) -> bool:
        return bool(self.lines)

    def __len__(self) -> int:
        return len(self.lines)


@dataclass
class HistoryBudget:
    """注入预算：条数与字符数都要有上限。"""

    max_lines: int = 30
    max_chars: int = 4000
    used_lines: int = 0
    used_chars: int = 0
    dropped: int = 0
    notes: list[str] = field(default_factory=list)


@runtime_checkable
class HistoryPort(Protocol):
    """历史只读端口。"""

    async def recent(self, session: str, *, limit: int = 30) -> HistorySlice:
        ...
