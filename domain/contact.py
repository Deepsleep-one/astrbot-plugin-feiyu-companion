"""主动联系候选（M4）。

谁可以被她主动找：**只有私聊里已绑定的主人**。
群聊永不产出候选——群聊里连来源都跨不过去，更不该变成主动联系人。
"""

from __future__ import annotations

from dataclasses import dataclass

from .context import SCENE_PRIVATE, InteractionContext


@dataclass(frozen=True)
class ContactCandidate:
    session_id: str
    user_id: str
    scene: str
    score: float = 0.0
    reason: str = ""
    topic: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "scene": self.scene,
            "score": self.score,
            "reason": self.reason,
            "topic": self.topic,
        }


def candidate_of(ctx: InteractionContext, *, topic: str = "") -> ContactCandidate | None:
    """把一个上下文换算成候选；不合适的直接返回 None。"""
    if ctx.scene != SCENE_PRIVATE:
        return None
    if not (ctx.is_owner and ctx.is_bound):
        return None
    return ContactCandidate(
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        scene=ctx.scene,
        score=1.0,
        reason="私聊已绑定主人",
        topic=topic,
    )


def select_candidates(
    contexts: list[InteractionContext] | tuple[InteractionContext, ...],
    *,
    topic: str = "",
    limit: int = 3,
) -> list[ContactCandidate]:
    """去重（同一会话只留一条）、保持顺序、截断。"""
    picked: dict[str, ContactCandidate] = {}
    for ctx in contexts:
        cand = candidate_of(ctx, topic=topic)
        if cand is None:
            continue
        kept = picked.get(cand.session_id)
        if kept is None or cand.score > kept.score:
            picked[cand.session_id] = cand
    ordered = sorted(picked.values(), key=lambda c: (-c.score, c.session_id))
    return ordered[:limit]
