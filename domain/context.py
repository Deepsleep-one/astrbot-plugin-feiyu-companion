"""交互上下文与来源引用（M3 契约层）。

这一层刻意不依赖 AstrBot、不依赖 SQLite：
契约先冻结，后面的运行时/适配器都照着它写，测试也不需要宿主。
"""

from __future__ import annotations

from dataclasses import dataclass

SCENE_PRIVATE = "private"
SCENE_GROUP = "group"

SOURCE_USER = "user_message"
SOURCE_ASSISTANT = "assistant_message"
SOURCE_SYSTEM = "system"
SOURCE_PROACTIVE = "proactive"
SOURCE_RECORD = "record"

SPEAKER_OWNER = "owner"
SPEAKER_ASSISTANT = "assistant"
SPEAKER_OTHER = "other"


class SourceLeak(PermissionError):
    """想把私聊里的来源带进群聊。"""


@dataclass(frozen=True)
class SourceRef:
    """一条具体来源：哪条消息、谁说的、能见范围。

    `scope` 是**可见范围**，不是场景：群聊里产生的东西 scope=group，
    私聊里产生的东西 scope=private。跨场景引用一律拒绝。
    """

    kind: str
    ref: str
    speaker: str = ""
    scope: str = SCENE_PRIVATE
    digest: str = ""
    ts: str = ""

    @property
    def is_private(self) -> bool:
        return self.scope == SCENE_PRIVATE

    @property
    def is_owner_words(self) -> bool:
        """只有主人自己说的话，才算主人的事实证据。"""
        return self.speaker == SPEAKER_OWNER and self.kind == SOURCE_USER

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "speaker": self.speaker,
            "scope": self.scope,
            "digest": self.digest,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SourceRef":
        return cls(
            kind=str(data.get("kind") or ""),
            ref=str(data.get("ref") or ""),
            speaker=str(data.get("speaker") or ""),
            scope=str(data.get("scope") or SCENE_PRIVATE),
            digest=str(data.get("digest") or ""),
            ts=str(data.get("ts") or ""),
        )


@dataclass(frozen=True)
class InteractionContext:
    """一次交互的完整上下文。权限、预算、检索都从这里取判断依据。"""

    platform: str
    bot_instance: str
    user_id: str
    session_id: str
    scene: str = SCENE_PRIVATE
    group_id: str = ""
    is_owner: bool = False
    is_bound: bool = False
    trace_id: str = ""
    now: str = ""
    source: SourceRef | None = None

    @property
    def is_private(self) -> bool:
        return self.scene == SCENE_PRIVATE

    @property
    def can_proactive(self) -> bool:
        """主动消息只发给私聊里已绑定的主人。"""
        return self.is_private and self.is_owner and self.is_bound

    def assert_visible(self, source: SourceRef | None) -> None:
        """群聊不许引用私聊来源；顺带禁止「来源没标场景」这种含糊状态。"""
        if source is None:
            return
        if source.scope not in (SCENE_PRIVATE, SCENE_GROUP):
            raise SourceLeak(f"来源场景不明（{source.scope!r}）：{source.ref}")
        if not self.is_private and source.is_private:
            raise SourceLeak(
                f"群聊 {self.session_id} 不能引用私聊来源 {source.ref}"
            )

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "bot_instance": self.bot_instance,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "scene": self.scene,
            "group_id": self.group_id,
            "is_owner": self.is_owner,
            "is_bound": self.is_bound,
            "trace_id": self.trace_id,
            "now": self.now,
            "source": self.source.to_dict() if self.source else None,
        }
