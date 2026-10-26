"""只读诊断工作台（M10-A）：把「现在到底能用什么」排成一张表。

`/陪伴工作台` 走这里。整条路径**只读**：不联网、不写库、不改配置、不发消息。
所有会变的东西（各适配器的状态、今天的用量）由 `main.py` 注入，这里只负责：

- 按固定的 13 项能力表拼矩阵；
- 把 `integration_pending` / `disabled` / 默认关 显式标出来（不许藏）；
- 抹掉令牌、路径、内网地址再回话；
- 顺带给出配置指纹（配置 revision 的短哈希）与四档权限说明。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from ..domain.context import InteractionContext
from ..domain.grants import (
    ALL_CAPABILITIES,
    CAP_BILI_LIKE,
    CAP_BILI_SHARE,
    CAP_CAMERA,
    CAP_DEVICE,
    CAP_GAME,
    CAP_IMAGE,
    CAP_LIVE,
    CAP_MEMORY_WRITE,
    CAP_PROACTIVE,
    CAP_SCREEN,
    CAP_TEXT,
    CAP_VOICE,
    CAP_WEB,
    PermissionPolicy,
)
from ..ports.workbench import (
    STATE_DELEGATED,
    STATE_DISABLED,
    STATE_OFF,
    STATE_ON,
    STATE_PENDING,
    CapabilityRow,
    Matrix,
    config_fingerprint,
    describe,
    redact,
    tier_table,
)

TZ = timezone(timedelta(hours=8))
DEFAULT_LIMIT = 40

#: 矩阵的固定顺序与「开关键 / 额度键」。
#: 能力名 —— 中文标签 —— 开关键 —— 额度键 —— 额度单位
ROW_SPEC: tuple[tuple[str, str, str, str, str], ...] = (
    (CAP_TEXT, "文字回复", "", "", ""),
    (CAP_PROACTIVE, "主动消息", "proactive_enable", "proactive_daily_limit", "条"),
    (CAP_VOICE, "语音", "voice_enable", "", ""),
    (CAP_IMAGE, "出图", "image_enable", "image_daily_limit", "张"),
    (CAP_WEB, "网页与天气", "web_enable", "web_daily_limit", "次"),
    (CAP_SCREEN, "识屏", "screen_enable", "screen_daily_limit", "次"),
    (CAP_CAMERA, "摄像头", "camera_enable", "camera_daily_limit", "次"),
    (CAP_DEVICE, "设备读数", "device_enable", "device_daily_limit", "次"),
    (CAP_LIVE, "直播只读", "live_enable", "live_daily_limit", "次"),
    (CAP_GAME, "文字小游戏", "game_enable", "game_daily_limit", "局"),
    (CAP_BILI_LIKE, "B站点赞", "", "", ""),
    (CAP_BILI_SHARE, "B站分享交接", "", "", ""),
    (CAP_MEMORY_WRITE, "写主人记忆", "", "", ""),
)

#: 这几项能力天生不由本插件的开关控制，状态由别处决定。
STATIC_NOTE = {
    CAP_TEXT: "跟随 AstrBot 正常对话，不受本插件的开关影响",
    CAP_BILI_LIKE: (
        "由 B站插件自己判断并执行（只对读过且有证据的、每日上限 3 条）；"
        "这边只读它今天的用量，不代点"
    ),
    CAP_BILI_SHARE: "影子模式只算不发；要交接得 mode=coordinated 且打开 relay",
    CAP_MEMORY_WRITE: "写主人事实的钥匙只在记忆插件手里，这边永远没有",
}

#: 没有开关可看的能力，默认按什么状态显示（有开关的走开关）。
NO_SWITCH_STATE = {
    CAP_TEXT: STATE_ON,
    #: 点赞是 B站插件的动作，不是「我们没接上」——所以是 delegated，不是 pending。
    CAP_BILI_LIKE: STATE_DELEGATED,
    CAP_BILI_SHARE: STATE_PENDING,
    CAP_MEMORY_WRITE: STATE_DISABLED,
}


def _now() -> datetime:
    return datetime.now(TZ)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


@dataclass
class WorkbenchPlan:
    """一次工作台读取的计划（构造好矩阵才能回话）。"""

    ok: bool
    reason: str = ""
    matrix: Matrix | None = None
    grant: object = None
    lines: list[str] = field(default_factory=list)


@dataclass
class WorkbenchOutcome:
    """回给主人的东西：一段话，没有附件。"""

    ok: bool
    reason: str = ""
    text: str = ""
    matrix: Matrix | None = None

    @property
    def needs_fallback(self) -> bool:
        return not self.ok


class WorkbenchTurn:
    """只读诊断的一回合：能看，不能改。"""

    def __init__(
        self,
        policy: PermissionPolicy,
        *,
        config: dict | None = None,
        limit: int = DEFAULT_LIMIT,
        clock=None,
    ) -> None:
        self.policy = policy
        self.config = dict(config or {})
        self.limit = max(8, int(limit))
        self.clock = clock
        self.calls = 0
        self.last_error = ""

    # ---- 基础 -------------------------------------------------------
    def _now(self) -> datetime:
        if self.clock is not None and hasattr(self.clock, "now"):
            return self.clock.now()
        return _now()

    def _gate(self, ctx: InteractionContext) -> str:
        if not ctx.is_private:
            return "工作台只在私聊里开：群里的每句话都有人在看"
        if not ctx.is_owner:
            return "工作台只给主人看"
        if not ctx.is_bound:
            return "还没认人：先在私聊里绑定，我才知道对面是你"
        return ""

    # ---- 拼矩阵 -----------------------------------------------------
    def _row(
        self,
        capability: str,
        label: str,
        switch_key: str,
        limit_key: str,
        unit: str,
        *,
        states: dict,
        usage: dict,
        limits: dict,
    ) -> CapabilityRow:
        state = states.get(capability)
        switch_on = _truthy(self.config.get(switch_key)) if switch_key else True
        limit_value = limits.get(capability, "")
        if not limit_value and limit_key:
            raw = self.config.get(limit_key)
            limit_value = f"{int(raw)} {unit}" if isinstance(raw, int) and raw > 0 else ""
        used = usage.get(capability, "")

        if switch_key and not switch_on:
            # 开关自己关着，就别拿「后台没接上」当理由：主人要看到的是「你关了」。
            state = STATE_OFF
        elif state is None:
            # 注入里没有这一项：有开关键就看开关；没有开关的按能力本身的性质定。
            if switch_key:
                state = STATE_ON
            else:
                state = NO_SWITCH_STATE.get(capability, STATE_ON)
        elif state in ("read_only", "verified_live"):
            state = STATE_ON
        elif state in ("integration_pending", "pending"):
            state = STATE_PENDING
        elif state in ("disabled", "off"):
            state = STATE_DISABLED if switch_key else STATE_OFF
        elif state in ("delegated", "elsewhere"):
            # 「这项由别人执行」——不是我们没接上。
            state = STATE_DELEGATED
        elif state not in (
            STATE_ON,
            STATE_OFF,
            STATE_PENDING,
            STATE_DISABLED,
            STATE_DELEGATED,
        ):
            # 适配器给了个没见过的状态：不许把怪值原样塞进矩阵，按「还没接上」处理并记账。
            self.last_error = f"{capability}:{state}"
            state = STATE_PENDING

        note = STATIC_NOTE.get(capability, "")
        if state == STATE_PENDING and not note:
            note = "契约在、后台没接上：现在问它只会如实说还没接"
        if state == STATE_OFF and switch_key:
            note = f"默认关：想用就在配置里打开 {switch_key}"
        return CapabilityRow(
            capability=capability,
            label=label,
            state=state,
            switch_key=switch_key,
            switch_on=switch_on,
            limit=limit_value,
            used=str(used or ""),
            note=note,
        )

    def build(
        self,
        ctx: InteractionContext,
        *,
        states: dict | None = None,
        usage: dict | None = None,
        limits: dict | None = None,
        now: datetime | None = None,
    ) -> WorkbenchPlan:
        """把矩阵拼出来。不发请求、不写库。"""
        moment = now or self._now()
        if not ctx.is_private:
            return WorkbenchPlan(False, self._gate(ctx))
        why = self._gate(ctx)
        if why:
            return WorkbenchPlan(False, why)
        states = {str(k): str(v) for k, v in dict(states or {}).items()}
        usage = dict(usage or {})
        limits = dict(limits or {})

        rows = [
            self._row(cap, label, switch, limit_key, unit,
                      states=states, usage=usage, limits=limits)
            for cap, label, switch, limit_key, unit in ROW_SPEC
        ]
        covered = {row.capability for row in rows}
        missing = [cap for cap in ALL_CAPABILITIES if cap not in covered]
        if missing:
            # 能力表加过而我们没在矩阵里排：这不是「看不见」，要当场说出来。
            for cap in missing:
                rows.append(
                    CapabilityRow(
                        capability=cap,
                        label=cap,
                        state=STATE_PENDING,
                        note="矩阵里还没登记这一项（代码走到新能力但没排进表）",
                    )
                )
        fingerprints = self._fingerprint()
        matrix = Matrix(
            rows=rows,
            generated_at=moment.isoformat(timespec="seconds"),
            fingerprint=fingerprints,
            notes=[
                "这张表只读：不改任何开关、不发任何消息。",
                "要改开关请自己改配置；要真发东西，得你在当场点头。",
            ],
        )
        try:
            grant = self.policy.grant_once(
                CAP_TEXT, scene=ctx.scene, reason="工作台只看状态"
            )
        except Exception as exc:  # noqa: BLE001
            grant = None
            self.last_error = type(exc).__name__
        self.calls += 1
        return WorkbenchPlan(
            True,
            matrix=matrix,
            grant=grant,
            lines=describe(matrix, limit=self.limit),
        )

    def _fingerprint(self) -> str:
        """配置 revision：整个配置算一个短指纹（只留指纹，不留值）。

        刻意不筛键：改任何一个值（包括额度）都该让指纹变，
        否则「线上跑的是哪一版」就对不上账。
        """
        return config_fingerprint(self.config)

    # ---- 回话 -------------------------------------------------------
    def render(self, plan: WorkbenchPlan) -> WorkbenchOutcome:
        if not plan.ok or plan.matrix is None:
            return WorkbenchOutcome(False, reason=plan.reason or "工作台没打开")
        body = list(plan.lines)
        body.append("")
        body.extend(tier_table())
        return WorkbenchOutcome(True, text="\n".join(redact(line) for line in body), matrix=plan.matrix)

    async def render_async(self, plan: WorkbenchPlan) -> WorkbenchOutcome:
        return self.render(plan)

    def status_line(self) -> str:
        return "只读诊断：能看全部 13 项能力的开关与状态；不改、不发、不联网"


__all__ = [
    "WorkbenchTurn",
    "WorkbenchPlan",
    "WorkbenchOutcome",
    "ROW_SPEC",
    "STATIC_NOTE",
]
