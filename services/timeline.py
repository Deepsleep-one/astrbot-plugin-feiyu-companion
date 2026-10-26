"""日程与主动时机（M4，仍然只出计划、不外发）。

这一层把三件事合起来回答「现在该不该找主人说话」：
1. 有没有可联系的人（只有私聊已绑定主人）；
2. 是不是免打扰时段（默认 23:00–07:00）；
3. 预算允不允许（每日条数、每分钟动作、失败退避）。

产出是 `Delivery(status=pending)` 的**计划**，没有任何发送实现。
模拟出来的生活只会被 `Activity.narrate()` 变成一句话，不可能变成真实动作。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..domain.activity import Activity
from ..domain.contact import ContactCandidate, candidate_of, select_candidates
from ..domain.context import InteractionContext
from ..domain.delivery import KIND_TEXT, STATUS_PENDING, Delivery
from ..domain.grants import CAP_PROACTIVE, PermissionPolicy, DEFAULT_POLICY
from ..runtime.clock import Clock, SystemClock
from .budget import Budget

QUIET_DEFAULT = ("23:00", "07:00")

#: (kind, title, 时, 分, 分钟数, 地点, 叙述句)
TEMPLATES: tuple[tuple[str, str, int, int, int, str, str], ...] = (
    ("routine", "起床，给窗台的花浇水", 7, 20, 30, "家里", "刚起来，给窗台那盆花浇了水。"),
    ("work", "坐在书桌前整理要读的东西", 9, 30, 90, "书桌前", "上午在书桌前坐了一会儿，把想读的东西排了个序。"),
    ("meal", "午饭：番茄鸡蛋面", 12, 10, 45, "厨房", "中午煮了番茄鸡蛋面，今天鸡蛋放多了。"),
    ("outdoor", "下楼走一圈，看看今天的云", 15, 30, 40, "小区", "下午下楼走了一圈，今天的云压得有点低。"),
    ("study", "窝在沙发上读书", 20, 0, 60, "沙发", "晚上窝在沙发上读了会儿书，差点睡着。"),
)


def parse_hhmm(text: str) -> time:
    raw = (text or "").strip()
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"时间格式应为 HH:MM，收到 {text!r}") from exc


def parse_quiet(spec: str | tuple[str, str] | None) -> tuple[time, time]:
    if spec is None:
        spec = QUIET_DEFAULT
    if isinstance(spec, str):
        parts = [p for p in spec.replace("—", "-").replace("~", "-").split("-") if p.strip()]
        if len(parts) != 2:
            raise ValueError(f"免打扰时段格式应为 HH:MM-HH:MM，收到 {spec!r}")
        return parse_hhmm(parts[0]), parse_hhmm(parts[1])
    start, end = spec
    return parse_hhmm(start), parse_hhmm(end)


@dataclass(frozen=True)
class DayPlan:
    day: str
    quiet: bool
    activities: tuple[Activity, ...] = ()
    candidates: tuple[ContactCandidate, ...] = ()
    deliveries: tuple[Delivery, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "quiet": self.quiet,
            "reason": self.reason,
            "dispatch": "planned_only",
            "activities": [a.to_dict() for a in self.activities],
            "candidates": [c.to_dict() for c in self.candidates],
            "deliveries": [d.to_dict() for d in self.deliveries],
        }


class Timeline:
    """一天怎么过、什么时候想说话，都由这里算。没有发送能力。"""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        budget: Budget | None = None,
        policy: PermissionPolicy | None = None,
        quiet: str | tuple[str, str] | None = None,
        templates: tuple[tuple[str, str, int, int, int, str, str], ...] | None = None,
        max_candidates: int = 3,
        max_planned: int | None = None,
    ) -> None:
        self.clock = clock or SystemClock()
        self.budget = budget or Budget(clock=self.clock)
        self.policy = policy or DEFAULT_POLICY
        self.quiet_start, self.quiet_end = parse_quiet(quiet)
        self.templates = templates if templates is not None else TEMPLATES
        self.max_candidates = max_candidates
        self.max_planned = max_planned

    # ---- 生活 -------------------------------------------------------------
    def activities_for(self, day: date) -> list[Activity]:
        tz = self.clock.now().tzinfo
        acts: list[Activity] = []
        for index, (kind, title, hh, mm, minutes, place, say) in enumerate(self.templates):
            start = datetime.combine(day, time(hh, mm), tzinfo=tz)
            acts.append(
                Activity(
                    id=f"act:{day.isoformat()}:{index}",
                    kind=kind,
                    title=title,
                    start=start,
                    end=start + timedelta(minutes=minutes),
                    place=place,
                    simulated=True,
                    say=say,
                )
            )
        return acts

    def current_activity(self, moment: datetime, day: date | None = None) -> Activity | None:
        acts = self.activities_for(day or moment.date())
        for act in acts:
            if act.covers(moment):
                return act
        upcoming = [a for a in acts if a.start > moment]
        if upcoming:
            return upcoming[0]
        return acts[-1] if acts else None

    # ---- 免打扰 -----------------------------------------------------------
    @property
    def quiet_label(self) -> str:
        """免打扰时段的可读写法，给状态页与日志用。"""
        return f"{self.quiet_start:%H:%M}-{self.quiet_end:%H:%M}"

    def is_quiet(self, moment: datetime) -> bool:
        start, end = self.quiet_start, self.quiet_end
        if start == end:
            return False
        clock_now = moment.timetz().replace(tzinfo=None)
        if start < end:
            return start <= clock_now < end
        return clock_now >= start or clock_now < end

    # ---- 该不该说话 -------------------------------------------------------
    def may_contact(self, ctx: InteractionContext, *, now: datetime | None = None) -> tuple[bool, str]:
        moment = now or self.clock.now()
        if candidate_of(ctx) is None:
            return False, "不是私聊已绑定主人"
        grant = self.policy.grant_for(ctx)
        if not grant.allows(CAP_PROACTIVE):
            return False, f"当前授权不含主动消息：{grant.reason}"
        if self.is_quiet(moment):
            return False, "免打扰时段"
        decision = self.budget.check(CAP_PROACTIVE)
        if not decision.allowed:
            return False, decision.reason
        return True, ""

    def plan_day(
        self,
        *,
        now: datetime | None = None,
        contexts: list[InteractionContext] | tuple[InteractionContext, ...] = (),
    ) -> DayPlan:
        moment = now or self.clock.now()
        day = moment.date()
        activities = tuple(self.activities_for(day))
        current = self.current_activity(moment, day)
        topic = current.title if current else ""

        unique: dict[str, InteractionContext] = {}
        for ctx in contexts:
            unique.setdefault(ctx.session_id, ctx)
        candidates = tuple(select_candidates(list(unique.values()), topic=topic, limit=self.max_candidates))

        if self.is_quiet(moment):
            return DayPlan(day=day.isoformat(), quiet=True, activities=activities,
                           candidates=candidates, reason="免打扰时段")
        if not candidates:
            return DayPlan(day=day.isoformat(), quiet=False, activities=activities,
                           reason="没有可联系的候选")

        deliveries: list[Delivery] = []
        reason = ""
        for ctx in unique.values():
            cand = candidate_of(ctx, topic=topic)
            if cand is None:
                continue
            grant = self.policy.grant_for(ctx)
            if not grant.allows(CAP_PROACTIVE):
                reason = f"授权不含主动消息：{grant.reason}"
                continue
            decision = self.budget.check(CAP_PROACTIVE)
            if not decision.allowed:
                reason = decision.reason
                break
            self.budget.consume(CAP_PROACTIVE)
            deliveries.append(self._planned(moment, ctx, cand, current, len(deliveries)))
            if self.max_planned and len(deliveries) >= self.max_planned:
                break

        if not deliveries and not reason:
            reason = "候选都没过授权"
        return DayPlan(
            day=day.isoformat(),
            quiet=False,
            activities=activities,
            candidates=candidates,
            deliveries=tuple(deliveries),
            reason=reason,
        )

    # ---- 内部 -------------------------------------------------------------
    def _planned(
        self,
        moment: datetime,
        ctx: InteractionContext,
        cand: ContactCandidate,
        activity: Activity | None,
        index: int,
    ) -> Delivery:
        text = activity.narrate() if activity else "想跟你说句话。"
        stamp = f"{moment.date().isoformat()}:{index}"
        return Delivery(
            id=f"dl:{stamp}:{ctx.session_id}",
            session_id=ctx.session_id,
            kind=KIND_TEXT,
            text=text,
            status=STATUS_PENDING,
            due_at=moment.isoformat(timespec="seconds"),
            reason="模拟生活叙述（只计划，未发送）",
            idem_key=f"proactive:{stamp}:{ctx.session_id}",
            created_at=moment.isoformat(timespec="seconds"),
            extra={
                "simulated": True,
                "activity_id": activity.id if activity else "",
                "topic": cand.topic,
            },
        )
