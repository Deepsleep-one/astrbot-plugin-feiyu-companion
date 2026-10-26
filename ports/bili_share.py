"""M6-C 第一段：分享权的「生成」契约（只 shadow，绝不发送）。

为什么单独一个端口而不是塞进 `BiliReadPortV1`：**读和写是两个授权**。
读端口可以随时被叫，分享端口一旦存在就必须能回答「谁在什么依据下决定要发」。
这一版只回答到「会发什么、为什么」，`executable` / `may_send` 恒 False。

三条硬规则（测试会逐条检查）：

1. 分享权只来自配置与主人授权，**永不来自证据**——证据只决定「够不够格提这条」；
2. `mode` 只认 `shadow`；`legacy` / `coordinated` 在这里一律拒绝（属于 M6-D 的握手范围）；
3. 每个决策都带一个 `generation`：同一天、同一份材料、同一条候选 → 同一个 generation，
   重复算一百次也只记一次账（这就是将来 ACK 握手的对账锚点）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

SHARE_PORT_NAME = "BiliSharePortV1"
GENERATION_VERSION = 1

MODE_LEGACY = "legacy"
MODE_SHADOW = "shadow"
MODE_COORDINATED = "coordinated"
SHARE_MODES = (MODE_LEGACY, MODE_SHADOW, MODE_COORDINATED)
SHADOW_MODES = (MODE_SHADOW,)

#: 决策动作。`send` 在 shadow 阶段只表示「真交接时它会发这一条」。
OP_SEND = "send"
OP_DEFER = "defer"
OP_SKIP = "skip"
OPS = (OP_SEND, OP_DEFER, OP_SKIP)

#: 决策里允许出现的字段（白名单）。多给的一律丢掉。
DECISION_FIELDS = (
    "generation",
    "bvid",
    "page",
    "op",
    "reason",
    "mode",
    "created_at",
    "executable",
    "would_send",
)


class ShareContractError(ValueError):
    """契约不成立：宁可拒绝，也不猜。"""


class ShareNotAllowed(PermissionError):
    """想真发：这一版没有这条路。"""


@dataclass(frozen=True)
class ShareDecision:
    """一条「如果现在真交接，我会怎么做」的判断。"""

    generation: str
    bvid: str
    page: int = 1
    op: str = OP_SKIP
    reason: str = ""
    mode: str = MODE_SHADOW
    created_at: str = ""
    #: 真交接前这里恒 False：决策不是执行。
    executable: bool = False

    def __post_init__(self) -> None:
        if self.op not in OPS:
            raise ShareContractError(f"未知分享决策：{self.op}")
        if self.mode not in SHARE_MODES:
            raise ShareContractError(f"未知分享模式：{self.mode}")
        if self.executable:
            raise ShareContractError("shadow 阶段的决策不能标成可执行")
        if not str(self.bvid or "").strip():
            raise ShareContractError("分享决策必须指名一个 BV")

    @property
    def would_send(self) -> bool:
        """「真交接时它会发这一条」——仍然不是「已经发了」。"""
        return self.op == OP_SEND

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "bvid": self.bvid,
            "page": int(self.page),
            "op": self.op,
            "reason": self.reason,
            "mode": self.mode,
            "created_at": self.created_at,
            "executable": False,
            "would_send": self.would_send,
        }


@dataclass(frozen=True)
class ShareReport:
    """一轮 shadow 的汇总。"""

    generation: str = ""
    mode: str = MODE_SHADOW
    created_at: str = ""
    decisions: tuple[ShareDecision, ...] = ()
    notes: tuple[str, ...] = ()
    executable: bool = False

    def counts(self) -> dict[str, int]:
        out = {OP_SEND: 0, OP_DEFER: 0, OP_SKIP: 0}
        for item in self.decisions:
            out[item.op] = out.get(item.op, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": GENERATION_VERSION,
            "generation": self.generation,
            "mode": self.mode,
            "created_at": self.created_at,
            "decisions": [item.to_dict() for item in self.decisions],
            "notes": list(self.notes),
            "executable": False,
        }


def generation_of(*, day: str, scope: str, bvid: str, page: int = 1, mode: str = MODE_SHADOW) -> str:
    """同一份材料永远得到同一个 generation（重复计算不重复记账）。"""
    payload = f"{GENERATION_VERSION}|{mode}|{day}|{scope}|{bvid}|{int(page)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def assert_shadow(mode: str) -> str:
    """这一版只认 shadow；别的模式一律明说「还没接」。"""
    value = str(mode or MODE_SHADOW).strip()
    if value not in SHARE_MODES:
        raise ShareContractError(f"未知分享模式：{value}")
    if value not in SHADOW_MODES:
        raise ShareNotAllowed(
            f"分享模式 {value} 还没实现：本阶段只做 shadow（生成判断、绝不发送）；"
            "真交接要等 M6-D 的 generation/ACK 握手"
        )
    return value


def refuse_send(reason: str = "") -> None:
    """任何「我想真发」的调用都从这里被挡住。"""
    raise ShareNotAllowed(
        "这一版不发送分享：只生成 shadow 判断"
        + (f"（{reason}）" if reason else "")
    )


def normalize_decision(raw: Mapping[str, Any], *, fallback_generation: str = "") -> ShareDecision:
    """只按白名单取字段；多的不看，缺的给保守默认。"""
    from .bili import redact  # 同一套脱敏，理由里不留 URL/凭证

    picked = {name: raw.get(name) for name in DECISION_FIELDS if name in raw}
    op = str(picked.get("op") or OP_SKIP)
    if op not in OPS:
        op = OP_SKIP
    try:
        page = int(picked.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    return ShareDecision(
        generation=str(picked.get("generation") or fallback_generation or ""),
        bvid=str(picked.get("bvid") or ""),
        page=max(1, page),
        op=op,
        reason=redact(str(picked.get("reason") or "")),
        mode=str(picked.get("mode") or MODE_SHADOW),
        created_at=str(picked.get("created_at") or ""),
        executable=False,  # 白名单里就算写了 True 也不认
    )


def digest(report: ShareReport) -> str:
    """整份报告的稳定指纹，供账本去重与审计对照。"""
    body = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
