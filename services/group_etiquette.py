"""M8-C：群聊里的表达——先学会什么时候不说话。

群聊一直是「一条能力都不给」。这里**不放开默认**：
- 只有 `group_reply_enabled=true`、且这个群在 `group_allowlist` 里、
  且（默认）被点名，才允许说一句**纯文字**；
- 语音 / 主动 / 出图 / 看图 / 网页 / 点赞 / 分享 / 写记忆，群里永远拿不到；
- 私聊来源在群里仍然会被拒（那是 `InteractionContext` 的契约，这里一个字都没动）。

判断顺序写死，理由要说人话：关着、不是群、不在白名单、免打扰、冷却、这段时间说太多、没被点名。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from ..domain.context import SCENE_GROUP, SCENE_PRIVATE
from ..domain.grants import CAP_TEXT, CapabilityGrant

WINDOW_SECONDS = 600.0
DEFAULT_MAX_PER_WINDOW = 3
DEFAULT_COOLDOWN = 60.0
DEFAULT_QUIET = (23, 7)
DEFAULT_NAMES = ("大肥鱼", "肥鱼", "小鱼")

# 群里永远不给的能力（这里再写一遍，是因为它比配置更硬）
GROUP_FORBIDDEN = frozenset(
    {
        "voice",
        "image",
        "proactive_message",
        "camera",
        "screen_share",
        "bili_like",
        "bili_share",
        "memory_write",
        "web_read",
    }
)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "开"}


def parse_allowlist(value) -> frozenset[str]:
    """`group_allowlist` 可以是列表、逗号/换行分隔的字符串。"""
    if value is None:
        return frozenset()
    if isinstance(value, (list, tuple, set)):
        rows = [str(item) for item in value]
    else:
        raw = str(value)
        for sep in ("\n", "，", "；", ";", "|"):
            raw = raw.replace(sep, ",")
        rows = raw.split(",")
    return frozenset(row.strip() for row in rows if row.strip())


def looks_mentioned(text: str, names=DEFAULT_NAMES) -> bool:
    """被点名了吗：`@名字`、开头叫名字、或整句里带名字且不超过 40 字。"""
    body = str(text or "").strip()
    if not body:
        return False
    lowered = body.lower()
    for name in names:
        token = str(name or "").strip()
        if not token:
            continue
        if f"@{token.lower()}" in lowered:
            return True
        if lowered.startswith(token.lower()):
            return True
        if token.lower() in lowered and len(body) <= 40:
            return True
    return False


@dataclass(frozen=True)
class GroupRule:
    enabled: bool = False
    allowlist: frozenset = field(default_factory=frozenset)
    max_per_window: int = DEFAULT_MAX_PER_WINDOW
    cooldown_seconds: float = DEFAULT_COOLDOWN
    quiet: tuple = DEFAULT_QUIET
    require_mention: bool = True
    names: tuple = DEFAULT_NAMES

    def __post_init__(self) -> None:
        if int(self.max_per_window) < 0:
            raise ValueError("群聊每分钟上限不能是负的")
        if float(self.cooldown_seconds) < 0:
            raise ValueError("群聊冷却不能是负的")


@dataclass(frozen=True)
class GroupVerdict:
    speak: bool
    reason: str = ""
    capabilities: frozenset = field(default_factory=frozenset)
    group_id: str = ""

    @property
    def grant(self) -> CapabilityGrant | None:
        """允许说话时给的单轮权限：只有文字，没有别的。"""
        if not self.speak:
            return None
        return CapabilityGrant(
            capabilities=frozenset({CAP_TEXT}),
            scene=SCENE_GROUP,
            reason="群聊白名单里被点名，只回一句文字",
            single_turn=True,
        )

    def to_dict(self) -> dict:
        return {
            "speak": bool(self.speak),
            "reason": self.reason,
            "capabilities": sorted(self.capabilities),
            "group_id": self.group_id,
        }


def rule_from_config(cfg: dict) -> GroupRule:
    return GroupRule(
        enabled=_truthy(cfg.get("group_reply_enabled", False)),
        allowlist=parse_allowlist(cfg.get("group_allowlist", "")),
        max_per_window=int(cfg.get("group_max_per_10min", DEFAULT_MAX_PER_WINDOW) or 0),
        cooldown_seconds=float(cfg.get("group_cooldown_seconds", DEFAULT_COOLDOWN) or 0),
        quiet=DEFAULT_QUIET,
        require_mention=_truthy(cfg.get("group_require_mention", True)),
    )


class GroupEtiquette:
    """一群一册的说话分寸：内存计账，重启归零（宁可少说）。"""

    def __init__(self, rule: GroupRule | None = None, *, clock=None, logger=None):
        self.rule = rule or GroupRule()
        self.clock = clock
        self.logger = logger
        self._spoken: dict[str, list[float]] = {}
        self._last: dict[str, float] = {}

    def _now(self, now: datetime | None) -> datetime:
        if now is not None:
            return now
        return self.clock.now() if self.clock is not None else datetime.now().astimezone()

    def _recent(self, group_id: str, stamp: float) -> list[float]:
        rows = [item for item in self._spoken.get(group_id, []) if stamp - item < WINDOW_SECONDS]
        self._spoken[group_id] = rows
        return rows

    def may_speak(
        self,
        ctx,
        *,
        mentioned: bool | None = None,
        text: str = "",
        now: datetime | None = None,
    ) -> GroupVerdict:
        scene = str(getattr(ctx, "scene", "") or "")
        group_id = str(getattr(ctx, "group_id", "") or "")
        if scene == SCENE_PRIVATE:
            return GroupVerdict(False, "这是私聊，不走群聊规则")
        if scene != SCENE_GROUP:
            return GroupVerdict(False, f"不认识的场景：{scene or '空'}")
        if not self.rule.enabled:
            return GroupVerdict(False, "群聊表达默认关（group_reply_enabled=false）", group_id=group_id)
        if not group_id:
            return GroupVerdict(False, "拿不到群号，先不说话", group_id=group_id)
        if group_id not in self.rule.allowlist:
            return GroupVerdict(False, f"这个群不在白名单里：{group_id}", group_id=group_id)

        moment = self._now(now)
        hour = moment.hour
        start, end = self.rule.quiet
        quiet = (hour >= start or hour < end) if start > end else (start <= hour < end)
        if quiet:
            return GroupVerdict(
                False, f"群聊免打扰时段（{start:02d}:00–{end:02d}:00）", group_id=group_id
            )

        stamp = moment.timestamp()
        last = self._last.get(group_id)
        if last is not None and stamp - last < self.rule.cooldown_seconds:
            left = int(self.rule.cooldown_seconds - (stamp - last))
            return GroupVerdict(False, f"刚说过，再等 {max(1, left)} 秒", group_id=group_id)

        recent = self._recent(group_id, stamp)
        if len(recent) >= self.rule.max_per_window:
            return GroupVerdict(
                False,
                f"这十分钟已经在群里说了 {len(recent)} 条（上限 {self.rule.max_per_window} 条）",
                group_id=group_id,
            )

        if self.rule.require_mention:
            body = text or str(getattr(ctx, "text", "") or "")
            hit = looks_mentioned(body, self.rule.names) if mentioned is None else bool(mentioned)
            if not hit:
                return GroupVerdict(False, "没被点名，先不说话（群聊只接话）", group_id=group_id)

        return GroupVerdict(
            True,
            "白名单群里被点名，回一句文字",
            capabilities=frozenset({CAP_TEXT}),
            group_id=group_id,
        )

    def note_spoken(self, group_id: str, *, now: datetime | None = None) -> None:
        moment = self._now(now)
        stamp = moment.timestamp()
        self._spoken.setdefault(group_id, []).append(stamp)
        self._last[group_id] = stamp

    def seen(self, group_id: str, *, now: datetime | None = None) -> int:
        moment = self._now(now)
        return len(self._recent(group_id, moment.timestamp()))

    def snapshot(self, now: datetime | None = None) -> dict:
        moment = self._now(now)
        stamp = moment.timestamp()
        return {
            "enabled": bool(self.rule.enabled),
            "allowlist": sorted(self.rule.allowlist),
            "max_per_window": int(self.rule.max_per_window),
            "cooldown_seconds": float(self.rule.cooldown_seconds),
            "require_mention": bool(self.rule.require_mention),
            "groups": {gid: len(self._recent(gid, stamp)) for gid in list(self._spoken)},
        }


__all__ = [
    "GROUP_FORBIDDEN",
    "GroupEtiquette",
    "GroupRule",
    "GroupVerdict",
    "DEFAULT_NAMES",
    "DEFAULT_MAX_PER_WINDOW",
    "DEFAULT_COOLDOWN",
    "DEFAULT_QUIET",
    "WINDOW_SECONDS",
    "looks_mentioned",
    "parse_allowlist",
    "rule_from_config",
]
