"""能力与授权（M3 契约层）。

默认值表就是安全边界，别在这里「顺手放开」：

| 场景 | 默认能力集 |
|---|---|
| 群聊 | 空集 |
| 私聊·非主人 | text_reply |
| 私聊·主人 | text_reply + proactive_message（还要过预算） |

`voice / camera / screen_share / bili_*` 一律**默认关闭**，
只能由主人当次明确请求拿一次性授权（`single_turn=True`）。
`memory_write` **永不可授**：主人事实只有记忆插件能写。
"""

from __future__ import annotations

from dataclasses import dataclass

from .context import SCENE_GROUP, SCENE_PRIVATE, InteractionContext

CAP_TEXT = "text_reply"
CAP_VOICE = "voice"
CAP_IMAGE = "image"
CAP_WEB = "web_read"
CAP_CAMERA = "camera"
CAP_SCREEN = "screen_share"
CAP_DEVICE = "device_read"
CAP_LIVE = "live_read"
CAP_GAME = "game_play"
CAP_PROACTIVE = "proactive_message"
CAP_BILI_LIKE = "bili_like"
CAP_BILI_SHARE = "bili_share"
CAP_MEMORY_WRITE = "memory_write"

ALL_CAPABILITIES = (
    CAP_TEXT,
    CAP_VOICE,
    CAP_IMAGE,
    CAP_WEB,
    CAP_CAMERA,
    CAP_SCREEN,
    CAP_DEVICE,
    CAP_LIVE,
    CAP_GAME,
    CAP_PROACTIVE,
    CAP_BILI_LIKE,
    CAP_BILI_SHARE,
    CAP_MEMORY_WRITE,
)

#: 任何情况下都不授予的能力。写主人事实的钥匙只在记忆插件手里。
NEVER_GRANTED = frozenset({CAP_MEMORY_WRITE})


class CapabilityDenied(PermissionError):
    """能力没授权，或授权形式不对（例如语音要单轮授权）。"""


@dataclass(frozen=True)
class CapabilityGrant:
    """一张授权凭据。`single_turn=True` 表示只对当前这一次请求有效。"""

    capabilities: frozenset[str]
    scene: str
    reason: str = ""
    single_turn: bool = False
    expires_at: str = ""

    def allows(self, capability: str) -> bool:
        return capability in self.capabilities

    def require(self, capability: str) -> None:
        if not self.allows(capability):
            raise CapabilityDenied(
                f"场景 {self.scene} 没有 {capability} 能力（{self.reason or '未授权'}）"
            )

    def to_dict(self) -> dict:
        return {
            "capabilities": sorted(self.capabilities),
            "scene": self.scene,
            "reason": self.reason,
            "single_turn": self.single_turn,
            "expires_at": self.expires_at,
        }


class PermissionPolicy:
    """把上下文换算成授权。所有默认值都收在这里，便于测试逐条钉住。"""

    def grant_for(self, ctx: InteractionContext) -> CapabilityGrant:
        if ctx.scene == SCENE_GROUP or not ctx.is_private:
            return CapabilityGrant(
                capabilities=frozenset(),
                scene=ctx.scene,
                reason="群聊默认不给额外能力",
            )
        caps = {CAP_TEXT}
        reason = "私聊文字回复"
        if ctx.is_owner and ctx.is_bound:
            caps.add(CAP_PROACTIVE)
            reason = "私聊主人：文字回复 + 主动陪伴（受预算约束）"
        elif ctx.is_owner:
            reason = "私聊主人但未绑定：只给文字回复"
        else:
            reason = "私聊非主人：只给文字回复"
        return CapabilityGrant(
            capabilities=frozenset(caps),
            scene=SCENE_PRIVATE,
            reason=reason,
        )

    def grant_once(
        self,
        capability: str,
        *,
        scene: str = SCENE_PRIVATE,
        reason: str = "主人当次明确要求",
        expires_at: str = "",
    ) -> CapabilityGrant:
        """为一次明确请求发单轮授权。用于「这次唱一首」这类请求。"""
        if capability in NEVER_GRANTED:
            raise CapabilityDenied(f"{capability} 不可授予：主人事实只能由记忆插件写入")
        if scene != SCENE_PRIVATE:
            raise CapabilityDenied(f"{capability} 只在私聊里可授予，当前场景 {scene}")
        return CapabilityGrant(
            capabilities=frozenset({CAP_TEXT, capability}),
            scene=scene,
            reason=reason,
            single_turn=True,
            expires_at=expires_at,
        )


DEFAULT_POLICY = PermissionPolicy()
