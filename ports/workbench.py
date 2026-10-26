"""只读诊断工作台（M10-A）。

这一层只做三件事，全部是纯函数、不联网、不写库：

1. **四档权限**：只读 / 诊断 / 试跑 / 真发。每一个动作先归到一档，档位不够就拒。
2. **能力矩阵**：13 项能力各自的开关、状态、额度，拼成一张能一眼看完的表。
   `integration_pending` / `disabled` / 默认关 —— 这些状态必须显式出现，不许藏在文案里。
3. **脱敏**：矩阵里不出现令牌、密钥、绝对路径、内网地址、账号与主机名。

矩阵里的开关值来自配置，状态来自各适配器（由 `main.py` 注入）。端口不 import
任何适配器，也不去读配置文件——它只负责把别人给的东西排成表、说人话。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 四档权限
TIER_READ_ONLY = "read_only"      # 只看状态，不改任何东西
TIER_DIAGNOSE = "diagnose"        # 多发几个只读请求（探一探通不通）
TIER_DRY_RUN = "dry_run"          # 真的跑一遍，但结果只给人看，不对外发
TIER_APPLY = "apply"              # 真的发出去 / 真的改配置

TIERS = (TIER_READ_ONLY, TIER_DIAGNOSE, TIER_DRY_RUN, TIER_APPLY)

TIER_LABELS = {
    TIER_READ_ONLY: "只读",
    TIER_DIAGNOSE: "诊断",
    TIER_DRY_RUN: "试跑",
    TIER_APPLY: "真发",
}

TIER_HINTS = {
    TIER_READ_ONLY: "只读状态，不碰外部",
    TIER_DIAGNOSE: "多打几个只读请求，看通不通",
    TIER_DRY_RUN: "真跑一遍，结果只给你看，不对外发",
    TIER_APPLY: "真的发出去或真的改配置，必须主人当场点头",
}

#: 动作 → 需要的档位。没有列出的动作一律按「真发」处理（宁可拒，不许瞎放）。
ACTION_TIERS = {
    "status": TIER_READ_ONLY,
    "matrix": TIER_READ_ONLY,
    "history": TIER_READ_ONLY,
    "diagnose": TIER_DIAGNOSE,
    "probe_agent": TIER_DIAGNOSE,
    "probe_memory": TIER_DIAGNOSE,
    "preview": TIER_DRY_RUN,
    "dry_run": TIER_DRY_RUN,
    "simulate": TIER_DRY_RUN,
    "send": TIER_APPLY,
    "share": TIER_APPLY,
    "like": TIER_APPLY,
    "change_config": TIER_APPLY,
    "restart": TIER_APPLY,
    "forget": TIER_APPLY,
    "delete": TIER_APPLY,
}


class WorkbenchError(ValueError):
    """矩阵本身不成立（比如能力名重复）。"""


class TierDenied(PermissionError):
    """档位不够。"""


def tier_of(action: str) -> str:
    """这个动作至少要哪一档。没登记的动作按最严的算。"""
    return ACTION_TIERS.get((action or "").strip().lower(), TIER_APPLY)


def tier_rank(tier: str) -> int:
    try:
        return TIERS.index(tier)
    except ValueError:
        return 0


def allow(action: str, granted: str) -> tuple[bool, str]:
    """能不能做这个动作；不能就给出人话理由。"""
    need = tier_of(action)
    if tier_rank(granted) < tier_rank(need):
        return (
            False,
            f"「{action}」要{TIER_LABELS.get(need, need)}档，现在是"
            f"{TIER_LABELS.get(granted, granted or '没给')}档：这一步得你自己点头。",
        )
    return True, ""


# ---------------------------------------------------------------- 脱敏
#: 这些词出现在长串里，说明它是文件名/插件名，不是令牌。
#: 备份清单要把文件名念给主人听，被自己的脱敏规则抹掉就白列了。
_NAME_MARKERS = (
    "plugin",
    ".bak",
    "feiyu",
    "config",
    "state",
    ".db",
    ".json",
    ".py",
    "_backups",
    ".png",
    ".log",
    ".md",
)


def _redact_token(match) -> str:
    token = match.group(0)
    low = token.lower()
    if any(marker in low for marker in _NAME_MARKERS):
        return token
    return "〔已隐去〕"


_REDACTIONS = (
    # 键值对：token=… / password: … / Authorization: Bearer …
    (re.compile(r"(?i)\b(token|secret|password|passwd|api[_-]?key|authorization)\b\s*[:=]\s*\S+"), r"\1=〔已隐去〕"),
    # 长 hex / base64 串（令牌、密钥、哈希）
    (re.compile(r"\b[0-9a-fA-F]{24,}\b"), "〔已隐去〕"),
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), _redact_token),
    # Windows / POSIX 绝对路径
    (re.compile(r"[A-Za-z]:\\[^\s，。；）)]+"), "〔路径已隐去〕"),
    (re.compile(r"(?<![\w.])/(?:opt|home|root|etc|var|tmp)/[^\s，。；）)]+"), "〔路径已隐去〕"),
    # 内网地址与端口
    (re.compile(r"\b(?:10|127|172\.(?:1[6-9]|2\d|3[01])|192\.168)(?:\.\d{1,3}){2,3}(?::\d+)?\b"), "〔内网地址已隐去〕"),
    # 邮箱与手机号
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "〔邮箱已隐去〕"),
    (re.compile(r"(?<!\d)1\d{10}(?!\d)"), "〔号码已隐去〕"),
)


def redact(text: str) -> str:
    """抹掉不该出现在诊断输出里的东西。"""
    out = str(text or "")
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    return out


# ---------------------------------------------------------------- 能力矩阵
STATE_ON = "on"
STATE_OFF = "off"
STATE_PENDING = "integration_pending"
STATE_DISABLED = "disabled"
#: 「这项由别人执行」：不是我们没接上，也不该被当成缺口（B站点赞就是这种）。
STATE_DELEGATED = "delegated"
STATES = (STATE_ON, STATE_OFF, STATE_PENDING, STATE_DISABLED, STATE_DELEGATED)

STATE_LABELS = {
    STATE_ON: "开着",
    STATE_OFF: "关着",
    STATE_PENDING: "还没接上",
    STATE_DISABLED: "停用",
    STATE_DELEGATED: "别处管",
}

#: 必须显式报出来的状态（工作台的「pending 单独一栏」就靠这个）。
NEEDS_ATTENTION = (STATE_PENDING, STATE_DISABLED)


@dataclass(frozen=True)
class CapabilityRow:
    """矩阵里的一行：一项能力现在到底能不能用。"""

    capability: str
    label: str
    state: str
    switch_key: str = ""
    switch_on: bool = False
    limit: str = ""
    used: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise WorkbenchError(f"未知状态 {self.state!r}：只认 {STATES}")

    @property
    def needs_attention(self) -> bool:
        return self.state in NEEDS_ATTENTION

    @property
    def usable(self) -> bool:
        """现在真能用吗：开着（或由别处执行），而且不是「还没接上」。"""
        return self.state in (STATE_ON, STATE_DELEGATED)

    def quota(self) -> str:
        """额度那一栏：有上限就写「已用/上限」，没有就留空。"""
        if self.limit and self.used:
            return f"{self.used}/{self.limit}"
        if self.limit:
            return f"上限 {self.limit}"
        return ""

    def to_dict(self) -> dict:
        return {
            "capability": self.capability,
            "label": self.label,
            "state": self.state,
            "switch_key": self.switch_key,
            "switch_on": bool(self.switch_on),
            "limit": self.limit,
            "used": self.used,
            "quota": self.quota(),
            "note": redact(self.note),
            "needs_attention": self.needs_attention,
            "usable": self.usable,
        }


@dataclass
class Matrix:
    """一张能力矩阵。"""

    rows: list[CapabilityRow] = field(default_factory=list)
    generated_at: str = ""
    fingerprint: str = ""
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        seen = set()
        for row in self.rows:
            if row.capability in seen:
                raise WorkbenchError(f"能力 {row.capability!r} 在矩阵里出现了两次")
            seen.add(row.capability)

    def by_state(self, state: str) -> list[CapabilityRow]:
        return [row for row in self.rows if row.state == state]

    @property
    def usable(self) -> list[CapabilityRow]:
        return [row for row in self.rows if row.usable]

    @property
    def attention(self) -> list[CapabilityRow]:
        return [row for row in self.rows if row.needs_attention]

    @property
    def switched_off(self) -> list[CapabilityRow]:
        return [row for row in self.rows if row.state == STATE_OFF]

    def counts(self) -> dict:
        return {
            "total": len(self.rows),
            "usable": len(self.usable),
            "off": len(self.switched_off),
            "attention": len(self.attention),
        }

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "fingerprint": self.fingerprint,
            "rows": [row.to_dict() for row in self.rows],
            "counts": self.counts(),
            "notes": [redact(n) for n in self.notes],
        }


# ---------------------------------------------------------------- 配置指纹
def config_fingerprint(config: dict, *, keys: list[str] | None = None) -> str:
    """配置 revision：只留指纹，不留值。

    同一个配置算出同一个短串，方便主人对着部署记录问「线上跑的是哪一版配置」；
    但指纹本身不能反推出令牌（sha256 前 12 位）。
    """
    data = dict(config or {})
    if keys:
        data = {k: data.get(k) for k in keys}
    blob = json.dumps(data, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def switch_summary(config: dict, keys: list[str]) -> dict:
    """只报开关的 True/False，不报值——给工作台与故障排查用。"""
    out = {}
    for key in keys:
        value = (config or {}).get(key)
        out[key] = value if isinstance(value, bool) else None
    return out


# ---------------------------------------------------------------- 说人话
def describe(matrix: Matrix, *, limit: int = 40) -> list[str]:
    """把矩阵说成人话，一行一项；太长的截断。"""
    counts = matrix.counts()
    lines = [
        f"能力矩阵（{counts['total']} 项：能用 {counts['usable']}，关着 {counts['off']}，"
        f"要理会 {counts['attention']}）"
    ]
    if matrix.fingerprint:
        lines.append(f"配置指纹：{matrix.fingerprint}（只留指纹，不留值）")
    for row in matrix.rows[: max(1, int(limit))]:
        quota = row.quota()
        tail = f"（{quota}）" if quota else ""
        lines.append(f"· {row.label}：{STATE_LABELS[row.state]}{tail}")
        if row.note and row.needs_attention:
            lines.append(f"  {redact(row.note)}")
    if matrix.attention:
        names = "、".join(row.label for row in matrix.attention)
        lines.append(f"要理会的：{names}")
    else:
        lines.append("没有待办的能力：所有 pending 都已接上。")
    for note in matrix.notes:
        lines.append(redact(note))
    return [redact(line) for line in lines]


def tier_table() -> list[str]:
    """四档权限的说明，给工作台第一屏用。"""
    lines = ["四档权限（从上到下越来越重）："]
    for tier in TIERS:
        lines.append(
            f"· {TIER_LABELS[tier]}（{tier}）：{TIER_HINTS[tier]}"
        )
    return lines


__all__ = [
    "TIER_READ_ONLY",
    "TIER_DIAGNOSE",
    "TIER_DRY_RUN",
    "TIER_APPLY",
    "TIERS",
    "TIER_LABELS",
    "TIER_HINTS",
    "ACTION_TIERS",
    "WorkbenchError",
    "TierDenied",
    "tier_of",
    "tier_rank",
    "allow",
    "redact",
    "STATE_ON",
    "STATE_OFF",
    "STATE_PENDING",
    "STATE_DISABLED",
    "STATE_DELEGATED",
    "STATES",
    "STATE_LABELS",
    "NEEDS_ATTENTION",
    "CapabilityRow",
    "Matrix",
    "config_fingerprint",
    "switch_summary",
    "describe",
    "tier_table",
]
