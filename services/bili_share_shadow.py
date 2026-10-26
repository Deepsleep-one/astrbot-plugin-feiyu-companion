"""M6-C 第一段：分享 shadow 评估器。

只做一件事：**在真交接之前，先把「她会怎么判断」变成可观察的数据**。

它不发送、不写对端的库、不申请额度、不改变任何 mode；输出只有
`ShareReport`（若干条 `ShareDecision`），每条都带 `executable=False`。

判断顺序是刻意排的（先硬约束、再软约束、最后看证据）：

1. 提议不在待批准状态 → skip
2. 今天已授权的分享额度用完了 → skip
3. 免打扰（B站 quiet，默认 23–09）→ defer
4. 距上次分享不到冷却时长 → defer
5. 连续若干条主人没回 → defer
6. 这条 BV 没有文本证据（官方字幕或机器转录）→ skip
7. 都过了 → send（一轮最多一条，后面的 defer）

第 6 条是「证据只决定够不够格，永不换授权」的落点：抽帧、metadata_only、
ASR 之外的画面都不算文本证据；就算算得上，也不加一条分享额度。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..ports.bili import BiliProposal, BiliSnapshot, redact
from ..ports.bili_share import (
    MODE_SHADOW,
    OP_DEFER,
    OP_SEND,
    OP_SKIP,
    ShareDecision,
    ShareReport,
    generation_of,
)

#: 文本证据：只有这两种算「真的看过内容」，抽帧不算。
TEXT_EVIDENCE = ("official_subtitle", "machine_transcript")

MAX_PER_ROUND = 1
MAX_UNANSWERED_DEFAULT = 4
COOLDOWN_DEFAULT = 120
QUIET_DEFAULT = (23, 9)
MAX_REASON = 160


@dataclass(frozen=True)
class ShadowContext:
    """评估用的现场（全部由调用方传入，本类不读时钟也不读配置）。"""

    now: datetime
    unanswered: int = 0
    last_share_at: datetime | None = None
    max_unanswered: int = MAX_UNANSWERED_DEFAULT
    cooldown_minutes: int = COOLDOWN_DEFAULT
    quiet: tuple[int, int] = QUIET_DEFAULT
    max_per_round: int = MAX_PER_ROUND


def _in_quiet(moment: datetime, quiet: tuple[int, int]) -> bool:
    start, end = quiet
    if start == end:
        return False
    hour = moment.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _minutes_since(moment: datetime, earlier: datetime | None) -> float | None:
    if earlier is None:
        return None
    try:
        return (moment - earlier).total_seconds() / 60.0
    except TypeError:  # naive/aware 混用：当作「不知道上次什么时候」，不因此放行
        return 0.0


def _evidence_index(snapshot: BiliSnapshot) -> dict[str, set[str]]:
    """bvid → 该 BV 上出现过的证据类型。"""
    out: dict[str, set[str]] = {}
    for reading in snapshot.readings:
        key = str(reading.bvid or "").strip()
        if not key:
            continue
        out.setdefault(key, set()).add(str(reading.evidence or "metadata_only"))
    return out


class ShadowEvaluator:
    """把「会怎么做」算出来，但什么都不做。"""

    def __init__(self, *, mode: str = MODE_SHADOW) -> None:
        if mode != MODE_SHADOW:
            raise ValueError(f"ShadowEvaluator 只接受 shadow 模式，收到：{mode}")
        self.mode = mode

    # ---- 单条判断 -------------------------------------------------
    def _decide(
        self,
        proposal: BiliProposal,
        *,
        snapshot: BiliSnapshot,
        ctx: ShadowContext,
        evidence: dict[str, set[str]],
        sent_already: int,
        day: str,
    ) -> ShareDecision:
        bvid = str(proposal.bvid or "").strip()
        page = max(1, int(proposal.page or 1))
        generation = generation_of(
            day=day, scope=snapshot.scope, bvid=bvid, page=page, mode=self.mode
        )

        def make(op: str, reason: str) -> ShareDecision:
            return ShareDecision(
                generation=generation,
                bvid=bvid,
                page=page,
                op=op,
                reason=redact(reason, MAX_REASON),
                mode=self.mode,
                created_at=ctx.now.isoformat(timespec="seconds"),
                executable=False,
            )

        state = str(proposal.state or "").strip()
        if state != "pending":
            return make(OP_SKIP, f"提议已经不在待批准状态（{state or '空'}）")

        runtime = snapshot.runtime
        if runtime.shares_left <= 0:
            return make(
                OP_SKIP,
                f"今天已授权的分享额度用完了（{runtime.shares_used}/{runtime.shares_limit}）",
            )

        if _in_quiet(ctx.now, ctx.quiet):
            start, end = ctx.quiet
            return make(OP_DEFER, f"免打扰时段（{start:02d}:00–{end:02d}:00），等之后再说")

        waited = _minutes_since(ctx.now, ctx.last_share_at)
        if waited is not None and waited < ctx.cooldown_minutes:
            left = max(0, ctx.cooldown_minutes - int(waited))
            return make(OP_DEFER, f"距上次分享不到 {ctx.cooldown_minutes} 分钟（还差 {left} 分钟）")

        if ctx.unanswered >= ctx.max_unanswered:
            return make(OP_DEFER, f"连续 {ctx.unanswered} 条没回，先不打扰")

        kinds = evidence.get(bvid, set())
        if not (kinds & set(TEXT_EVIDENCE)):
            seen = "、".join(sorted(kinds)) or "没有阅读记录"
            return make(OP_SKIP, f"这条没有文本证据（{seen}），不拿分享")

        if sent_already >= ctx.max_per_round:
            return make(OP_DEFER, f"这一轮已经排了 {sent_already} 条，剩下的下一轮再看")

        return make(OP_SEND, "够格：待批准的提议 + 有文本证据 + 额度与时段都允许")

    # ---- 一轮 -----------------------------------------------------
    def evaluate(self, snapshot: BiliSnapshot | None, ctx: ShadowContext) -> ShareReport:
        if snapshot is None:
            return ShareReport(
                mode=self.mode,
                created_at=ctx.now.isoformat(timespec="seconds"),
                notes=("没有快照：桥没接通或没开",),
            )

        day = ctx.now.date().isoformat()
        evidence = _evidence_index(snapshot)
        decisions: list[ShareDecision] = []
        sent_already = 0

        for proposal in snapshot.proposals:
            if not str(proposal.bvid or "").strip():
                continue
            item = self._decide(
                proposal,
                snapshot=snapshot,
                ctx=ctx,
                evidence=evidence,
                sent_already=sent_already,
                day=day,
            )
            if item.op == OP_SEND:
                sent_already += 1
            decisions.append(item)

        notes: list[str] = []
        if not decisions:
            notes.append("没有可评估的提议")
        chosen = next((d for d in decisions if d.op == OP_SEND), None)
        if chosen is None:
            notes.append("这一轮没有会发出去的分享")
        generation = chosen.generation if chosen else ""

        return ShareReport(
            generation=generation,
            mode=self.mode,
            created_at=ctx.now.isoformat(timespec="seconds"),
            decisions=tuple(decisions),
            notes=tuple(notes),
            executable=False,
        )


def describe(report: ShareReport | None, *, limit: int = 6) -> str:
    """给人看的一小段；只描述判断，不出现任何执行暗示。"""
    if report is None:
        return "还没有影子判断。"
    counts = report.counts()
    lines = [
        "影子判断："
        f"会发 {counts.get(OP_SEND, 0)} 条 / 押后 {counts.get(OP_DEFER, 0)} 条 / "
        f"放弃 {counts.get(OP_SKIP, 0)} 条"
    ]
    for item in report.decisions[:limit]:
        mark = {OP_SEND: "会发", OP_DEFER: "押后", OP_SKIP: "放弃"}.get(item.op, item.op)
        lines.append(f"· {item.bvid}（第 {item.page} 集）{mark}：{item.reason}")
    if len(report.decisions) > limit:
        lines.append(f"· 还有 {len(report.decisions) - limit} 条没列出来")
    for note in report.notes:
        lines.append(f"· {note}")
    lines.append("这一条只是影子：没有发送、没有申请额度、没有改对端任何状态。")
    return "\n".join(lines)
