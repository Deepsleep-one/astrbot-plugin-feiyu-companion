"""B站只读诊断的文字化（M6-A）。

只做一件事：把 `BiliSnapshot` 摊成几行给主人看。**不判断、不决定、不发送。**
特别要守住的两句话会固定出现在输出里：

- 「这些数是瞬间视图，不是可执行余额」——读数不能当额度用；
- 「分享权来自授权，不从证据推断」——再充分的字幕/转写也买不来一条分享。
"""

from __future__ import annotations

from ..ports.bili import BiliSnapshot

BALANCE_CAVEAT = "这些是瞬间视图，不是可执行余额；要动额度仍走 B 站自己的授权与去重。"
SHARE_CAVEAT = "分享权来自配置与主人授权，证据再充分也不换一条分享。"


def bridge_line(state: str, detail: str) -> str:
    return f"B站只读桥：{state}（{detail}）"


def describe(snapshot: BiliSnapshot, *, limit: int = 14) -> list[str]:
    counts = snapshot.counts()
    lines: list[str] = []

    if counts["readings"]:
        lines.append(
            f"阅读证据：{counts['readings']} 条（读过 {counts['read']}、"
            f"看完整 {counts['full']}、够得上点赞判断 {counts['like_ready']}）"
        )
    else:
        lines.append("阅读证据：0 条")

    runtime = snapshot.runtime
    flags = [
        f"自动探索={'开' if runtime.auto else '关'}",
        f"点赞许可={'开' if runtime.likes_enabled else '关'}",
    ]
    if runtime.next_wake:
        flags.append(f"下次唤醒={runtime.next_wake}")
    if runtime.round:
        flags.append(f"第 {runtime.round} 轮")
    lines.append("运行：" + "，".join(flags))

    lines.append(
        f"今日用量：赞 {runtime.likes_used}/{runtime.likes_limit or '—'}，"
        f"分享 {runtime.shares_used}/{runtime.shares_limit or '—'}，"
        f"ASR {runtime.asr_used}/{runtime.asr_limit or '—'}"
        + ("（转写中）" if runtime.asr_busy else "")
    )
    if runtime.last_error_type:
        lines.append(f"最近一次错误类型：{runtime.last_error_type}（只留类型，不留原文）")

    if snapshot.reflections:
        topics = "、".join(r.topic or "未标主题" for r in snapshot.reflections[:5])
        lines.append(f"有效认识：{len(snapshot.reflections)} 条（{topics}）")
    else:
        lines.append("有效认识：0 条")

    if snapshot.interests:
        top = "、".join(f"{i.topic}({i.weight:.2f})" for i in snapshot.interests[:3])
        lines.append(f"兴趣：{len(snapshot.interests)} 项，前几项 {top}")
    else:
        lines.append("兴趣：0 项")

    if snapshot.proposals:
        shown = "、".join(
            f"{p.proposal_id[-8:] or p.proposal_id}→{p.bvid or '无BV'}"
            for p in snapshot.proposals[:3]
        )
        lines.append(f"待批准提议：{len(snapshot.proposals)} 条（{shown}）")
    else:
        lines.append("待批准提议：0 条")

    if snapshot.dropped:
        reason = "、".join(f"{name} {count}" for name, count in sorted(snapshot.dropped.items()))
        lines.append(f"没进上下文的材料：{reason}")

    for note in snapshot.notes[:2]:
        lines.append(f"· {note}")

    lines.append(BALANCE_CAVEAT)
    lines.append(SHARE_CAVEAT)

    # 「看完整」这一条要能单独看见，避免有人拿抽帧当整片
    partial = [r for r in snapshot.readings if r.is_read and not r.fully_watched]
    if partial:
        lines.append(f"提醒：{len(partial)} 条只是部分覆盖/抽帧，不等于看完整个视频。")

    return lines[:limit]
