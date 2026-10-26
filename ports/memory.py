"""记忆端口（M3 只读面）。

约定：
- companion 只通过这个端口读记忆，**永远不直接开 SQLite 写记忆库**；
- 写主人事实的唯一入口仍然是记忆插件自己的 `add_record` / `correct_record`；
- 端口没有 `propose_memory` 之外的写方法，且实现在 M3 里直接抛 `PortNotWired`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class PortNotWired(RuntimeError):
    """端口还没接线（例如写入面要等记忆插件提供正式接口）。"""


@dataclass(frozen=True)
class RecallQuery:
    text: str
    owner_id: str
    scene: str
    is_owner: bool = False
    user_id: str = ""
    group_id: str = ""
    limit: int = 4
    min_score: float = 2.5
    vector: list[float] | None = None
    vector_meta: dict | None = None


@dataclass(frozen=True)
class RecallHit:
    record_id: str
    content: str
    kind: str = ""
    epistemic_type: str = ""
    scope_type: str = ""
    score: float = 0.0
    asserted_at: str = ""
    revision: int = 1
    source_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "content": self.content,
            "kind": self.kind,
            "epistemic_type": self.epistemic_type,
            "scope_type": self.scope_type,
            "score": self.score,
            "asserted_at": self.asserted_at,
            "revision": self.revision,
            "source_ids": list(self.source_ids),
        }


@dataclass(frozen=True)
class RecallResult:
    hits: list[RecallHit] = field(default_factory=list)
    trace: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.hits)


@dataclass(frozen=True)
class ChangeEvent:
    record_id: str
    kind: str  # upserted | gone
    revision: int = 0
    updated_at: str = ""


@dataclass(frozen=True)
class SourceCheck:
    ok: bool
    reason: str = ""
    missing: tuple[str, ...] = ()


class MemoryPort(Protocol):
    def recall(self, query: RecallQuery) -> RecallResult: ...

    def changes(self, since: str, *, limit: int = 200) -> list[ChangeEvent]: ...

    def validate_sources(self, record_ids: list[str]) -> SourceCheck: ...
