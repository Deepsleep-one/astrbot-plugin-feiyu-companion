"""把记忆插件的 `MemoryStore` 包成只读端口。

**这不是双向集成**：适配器只读，写入面是断的（`propose_memory` 抛 `PortNotWired`），
因为主人事实的唯一权威在记忆插件里，companion 不能自己写。

已知语义差异（写在这里免得以后误判）：
`MemoryStore.list_records(since=...)` 过滤的是 `asserted_at`，不是 `updated_at`，
所以「变更流」只能尽力而为：先按 since 取一次，再扫最近一批按 `updated_at` 补漏。
删除事件靠 `status == "deleted"` 判定。
"""

from __future__ import annotations

from typing import Any

from ..ports.memory import (
    ChangeEvent,
    PortNotWired,
    RecallHit,
    RecallQuery,
    RecallResult,
    SourceCheck,
)


class MemoryStoreAdapter:
    """`store` 是记忆插件的 `MemoryStore` 实例（同一进程内传入，不复制库）。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    # ---- 读 ---------------------------------------------------------------
    def recall(self, query: RecallQuery) -> RecallResult:
        stats: dict = {}
        rows = self._store.search(
            query.text,
            owner_id=query.owner_id,
            is_owner=query.is_owner,
            scene=query.scene,
            group_id=query.group_id,
            user_id=query.user_id,
            limit=query.limit,
            min_score=query.min_score,
            vector=query.vector,
            vector_meta=query.vector_meta,
            stats=stats,
        )
        hits = [
            RecallHit(
                record_id=str(row.get("id") or ""),
                content=str(row.get("content") or ""),
                kind=str(row.get("kind") or ""),
                epistemic_type=str(row.get("epistemic_type") or ""),
                scope_type=str(row.get("scope_type") or ""),
                score=float(row.get("_score") or 0.0),
                asserted_at=str(row.get("asserted_at") or ""),
                revision=int(row.get("revision") or 1),
                source_ids=tuple(_load_ids(row.get("source_ids"))),
            )
            for row in rows
        ]
        return RecallResult(hits=hits, trace=dict(stats))

    def changes(self, since: str, *, limit: int = 200) -> list[ChangeEvent]:
        seen: dict[str, ChangeEvent] = {}
        candidates: list[dict] = []
        if since:
            candidates.extend(
                self._store.list_records(status=None, since=since, limit=limit)
            )
        candidates.extend(self._store.list_records(status=None, limit=limit))
        for row in candidates:
            rid = str(row.get("id") or "")
            if not rid or rid in seen:
                continue
            updated = str(row.get("updated_at") or row.get("asserted_at") or "")
            if since and updated and updated < since:
                continue
            kind = "gone" if str(row.get("status")) == "deleted" else "upserted"
            seen[rid] = ChangeEvent(
                record_id=rid,
                kind=kind,
                revision=int(row.get("revision") or 0),
                updated_at=updated,
            )
        return sorted(seen.values(), key=lambda e: (e.updated_at, e.record_id))

    def validate_sources(self, record_ids: list[str]) -> SourceCheck:
        ok, reason = self._store.validate_sources(list(record_ids))
        missing = () if ok else tuple(str(x) for x in record_ids)
        return SourceCheck(ok=bool(ok), reason=str(reason or ""), missing=missing)

    # ---- 写面是断的 -------------------------------------------------------
    def propose_memory(self, *args: Any, **kwargs: Any) -> None:
        raise PortNotWired("只有记忆插件能写主人事实：companion 不提供写入面")

    def add_record(self, *args: Any, **kwargs: Any) -> None:
        raise PortNotWired("只有记忆插件能写主人事实：companion 不提供写入面")


def _load_ids(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    text = str(raw).strip()
    if not text:
        return []
    try:
        import json

        data = json.loads(text)
        if isinstance(data, list):
            return [str(x) for x in data]
    except (ValueError, TypeError):
        pass
    return [text]
