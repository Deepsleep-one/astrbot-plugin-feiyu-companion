"""B站只读端口（M6-A：`BiliReadPortV1` 契约 + 白名单 DTO + 受众/证据/提议校验）。

这个端口只做三件事：

1. **定义契约**：`BiliReadPortV1` 的入参（`BiliQuery`）与出参（`BiliSnapshot`）；
2. **把外部数据当不可信输入**：`normalize_snapshot()` 按白名单逐字段取，
   多出来的键（cookie、token、整份 settings、私聊历史……）一律丢掉；
3. **在桥这一层补上原插件的已知洞**（见 `M6_B站接入与配置方案.md` §7）：
   同 BV 跨 scope 串味、撤回后回填旧手账、提议用序号当 ID、把「元数据」说成「已读」、
   把「机器转写/抽帧」当成「可以买分享权」。

刻意**不做**的事：不 import B站插件的任何内部模块，不打开它那边的库文件，
不实例化任何阅读存储对象（那会建表、迁移、写设置、过期待发、读凭证），
不联网、不调模型、不发消息。桥没接线时一律抛 `BiliUnavailable`，
外面按 `integration_pending` 显示——**不许假装读到了**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

PORT_NAME = "BiliReadPortV1"
SNAPSHOT_VERSION = 1

SCENE_PRIVATE = "private"

#: 证据强度（越靠前越「真看过」）。`metadata_only` 只证明「拿到过条目」，不证明「读过」。
EVIDENCE_ORDER = ("official_subtitle", "machine_transcript", "mixed", "frames", "metadata_only")
SUBTITLE_STATES = ("official", "asr", "none", "unknown")
COVERAGE = ("full", "partial", "none")
REFLECTION_STATES = ("active", "superseded", "withdrawn")
PROPOSAL_STATES = ("pending", "approved", "rejected", "expired")

#: 白名单：只取这些键，其余全部丢弃。
READING_FIELDS = (
    "reading_id", "bvid", "page", "cid", "revision", "fetched_at",
    "evidence", "subtitle_state", "coverage", "scope",
)
REFLECTION_FIELDS = (
    "reflection_id", "journal_id", "topic", "text", "state", "supersedes",
    "memory_id", "source_refs", "revision", "scope", "updated_at", "source",
)
INTEREST_FIELDS = ("topic", "weight", "updated_at")
PROPOSAL_FIELDS = ("proposal_id", "bvid", "page", "state", "revision", "created_at", "scope")
RUNTIME_FIELDS = (
    "auto", "likes_enabled", "next_wake", "round",
    "likes_used", "likes_limit", "shares_used", "shares_limit",
    "asr_used", "asr_limit", "asr_busy", "asr_reserved",
    "share_cooldown_minutes", "quiet_start", "quiet_end", "last_error_type",
)

#: 分享权只能来自配置与主人授权，**永远不来自证据**。
SHARE_RIGHTS_FROM_EVIDENCE = False


class BiliUnavailable(RuntimeError):
    """桥没接线：没有插件、没有 facade、或模式还没实现。外面按 integration_pending 显示。"""


class BiliContractError(ValueError):
    """对端返回的东西不符合契约（版本不对、必须字段缺失、用序号冒充 ID……）。"""


# --------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------

_REDACTIONS = (
    (re.compile(r"https?://\S+", re.IGNORECASE), "[url]"),
    (
        re.compile(
            r"(?i)\b(SESSDATA|bili_jct|DedeUserID__ckMd5|access_key|refresh_token|"
            r"token|api[_-]?key|cookie|password)\s*[=:]\s*[^\s;&,]+"
        ),
        r"\1=[已隐去]",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), "Bearer [已隐去]"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "[hash]"),
)

_ERROR_KIND = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Timeout))\b")


def redact(text: object, limit: int = 200) -> str:
    """把字符串里像 URL / 凭证 / 一长串哈希的东西抹掉，再截断。"""
    out = str(text or "")
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    out = out.strip()
    if limit and len(out) > limit:
        out = out[: max(0, limit - 1)].rstrip() + "…"
    return out


def error_kind(text: object) -> str:
    """只留错误**类型**（`TimeoutError` 这种），不留原文。"""
    cleaned = redact(text, limit=0)
    match = _ERROR_KIND.search(cleaned)
    if match:
        return match.group(1)
    return "unknown"


# --------------------------------------------------------------------------
# 入参
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BiliQuery:
    """一次只读查询的范围。

    **必须由宿主按绑定关系构造**，不能让模型自己填 owner / 会话 / 能力集合——
    模型能改的只是 `topic` 这类检索偏好。
    """

    owner_id: str
    scene: str = SCENE_PRIVATE
    platform: str = ""
    bot_instance: str = ""
    session_id: str = ""
    scope: str = ""
    topic: str = ""
    max_readings: int = 10
    max_reflections: int = 4
    max_interests: int = 8
    max_proposals: int = 5
    max_age_hours: int = 72

    def __post_init__(self) -> None:
        if not str(self.owner_id or "").strip():
            raise BiliContractError("只读查询必须有 owner_id：不接受匿名受众")
        if self.scene != SCENE_PRIVATE:
            raise BiliContractError(f"只读桥只服务私聊，收到场景：{self.scene}")

    @property
    def effective_scope(self) -> str:
        """期望的 scope 串。宿主给了就用宿主的，否则按 platform/bot/session 拼一个。"""
        if self.scope:
            return self.scope
        parts = [self.platform, self.bot_instance, self.session_id]
        if all(parts):
            return ":".join(parts)
        return ""

    def to_dict(self) -> dict:
        return {
            "port": PORT_NAME,
            "version": SNAPSHOT_VERSION,
            "owner_id": self.owner_id,
            "scene": self.scene,
            "platform": self.platform,
            "bot_instance": self.bot_instance,
            "session_id": self.session_id,
            "scope": self.effective_scope,
            "topic": self.topic,
            "max_readings": self.max_readings,
            "max_reflections": self.max_reflections,
            "max_interests": self.max_interests,
            "max_proposals": self.max_proposals,
            "max_age_hours": self.max_age_hours,
        }


# --------------------------------------------------------------------------
# 出参（白名单 DTO）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BiliReading:
    reading_id: str
    bvid: str
    page: int = 1
    cid: str = ""
    revision: int = 1
    fetched_at: str = ""
    evidence: str = "metadata_only"
    subtitle_state: str = "unknown"
    coverage: str = "none"
    scope: str = ""

    @property
    def is_read(self) -> bool:
        """拿到条目 ≠ 读过。只有真读过的证据才算读。"""
        return self.evidence in ("official_subtitle", "machine_transcript", "mixed", "frames")

    @property
    def fully_watched(self) -> bool:
        """抽帧/部分覆盖都不算看完整视频。"""
        return self.evidence in ("official_subtitle", "machine_transcript", "mixed") and self.coverage == "full"

    @property
    def may_judge_like(self) -> bool:
        """够得上参与点赞判断的最低证据：不是纯元数据，且至少有一点覆盖。"""
        return self.is_read and self.coverage in ("full", "partial")

    @property
    def may_grant_share(self) -> bool:
        """永远 False：分享权来自配置与主人授权，不从证据推断。"""
        return SHARE_RIGHTS_FROM_EVIDENCE

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in READING_FIELDS}


@dataclass(frozen=True)
class BiliReflection:
    reflection_id: str
    text: str
    topic: str = ""
    journal_id: str = ""
    state: str = "active"
    supersedes: str = ""
    memory_id: str = ""
    source_refs: tuple[str, ...] = ()
    revision: int = 1
    scope: str = ""
    updated_at: str = ""
    source: str = "reflection"

    def to_dict(self) -> dict:
        out = {name: getattr(self, name) for name in REFLECTION_FIELDS}
        out["source_refs"] = list(self.source_refs)
        return out


@dataclass(frozen=True)
class BiliInterest:
    topic: str
    weight: float = 0.0
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in INTEREST_FIELDS}


@dataclass(frozen=True)
class BiliProposal:
    proposal_id: str
    bvid: str = ""
    page: int = 1
    state: str = "pending"
    revision: int = 1
    created_at: str = ""
    scope: str = ""

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in PROPOSAL_FIELDS}


@dataclass(frozen=True)
class BiliRuntime:
    """运行摘要。**只是一瞬间的视图，不是可执行余额。**"""

    auto: bool = False
    likes_enabled: bool = False
    next_wake: str = ""
    round: int = 0
    likes_used: int = 0
    likes_limit: int = 0
    shares_used: int = 0
    shares_limit: int = 0
    asr_used: int = 0
    asr_limit: int = 0
    asr_busy: bool = False
    asr_reserved: int = 0
    share_cooldown_minutes: int = 0
    quiet_start: int = 0
    quiet_end: int = 0
    last_error_type: str = ""

    @property
    def executable(self) -> bool:
        """读数不能当余额用：这个属性永远是 False。"""
        return False

    @property
    def shares_left(self) -> int:
        if self.shares_limit <= 0:
            return 0
        return max(0, self.shares_limit - self.shares_used)

    @property
    def likes_left(self) -> int:
        if self.likes_limit <= 0:
            return 0
        return max(0, self.likes_limit - self.likes_used)

    def to_dict(self) -> dict:
        out = {name: getattr(self, name) for name in RUNTIME_FIELDS}
        out["executable"] = False
        return out


@dataclass(frozen=True)
class BiliSnapshot:
    version: int = SNAPSHOT_VERSION
    queried_at: str = ""
    scope: str = ""
    readings: tuple[BiliReading, ...] = ()
    reflections: tuple[BiliReflection, ...] = ()
    interests: tuple[BiliInterest, ...] = ()
    proposals: tuple[BiliProposal, ...] = ()
    runtime: BiliRuntime = field(default_factory=BiliRuntime)
    dropped: dict = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def counts(self) -> dict:
        return {
            "readings": len(self.readings),
            "read": sum(1 for r in self.readings if r.is_read),
            "full": sum(1 for r in self.readings if r.fully_watched),
            "like_ready": sum(1 for r in self.readings if r.may_judge_like),
            "reflections": len(self.reflections),
            "interests": len(self.interests),
            "proposals": len(self.proposals),
            "dropped": sum(int(v) for v in self.dropped.values()),
        }

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "queried_at": self.queried_at,
            "scope": self.scope,
            "readings": [r.to_dict() for r in self.readings],
            "reflections": [r.to_dict() for r in self.reflections],
            "interests": [i.to_dict() for i in self.interests],
            "proposals": [p.to_dict() for p in self.proposals],
            "runtime": self.runtime.to_dict(),
            "dropped": dict(self.dropped),
            "notes": list(self.notes),
        }


class BiliReadPortV1(Protocol):
    def snapshot(self, query: BiliQuery) -> BiliSnapshot: ...


# --------------------------------------------------------------------------
# 归一化（把不可信的外部 dict 变成白名单 DTO）
# --------------------------------------------------------------------------


def _pick(raw: dict, fields: tuple[str, ...]) -> dict:
    return {name: raw[name] for name in fields if name in raw}


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off", ""):
            return False
    return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_str(value: object, limit: int = 400) -> str:
    text = str(value or "").strip()
    return text[:limit] if limit and len(text) > limit else text


def _as_refs(value: object, limit: int = 8) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
    else:
        items = []
    cleaned = [_as_str(v, 120) for v in items]
    return tuple([v for v in cleaned if v][:limit])


def _parse_dt(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


_ORDINAL = re.compile(r"^#?\d+$")


def normalize_snapshot(raw: object, query: BiliQuery) -> BiliSnapshot:
    """把对端返回的 dict 变成 `BiliSnapshot`。

    失败策略：
    - **整份不合格**（不是 dict、版本不对、提议用序号当 ID）→ 抛 `BiliContractError`；
    - **单条不合格**（scope 串味、已撤回、时间太旧、字段不合法）→ 丢掉并计数，
      不让一条脏数据把整次诊断打挂。
    """
    if not isinstance(raw, dict):
        raise BiliContractError(f"只读桥必须返回 dict，收到：{type(raw).__name__}")

    version = _as_int(raw.get("version"), 0)
    if version != SNAPSHOT_VERSION:
        raise BiliContractError(f"只读桥版本不符：{version}，本端只认 {SNAPSHOT_VERSION}")

    # 校验用的 scope 只认**调用方显式声明**的那个：没声明就没法判串味，
    # 只能放行并记一笔（effective_scope 的兜底值只用于展示，不能当校验依据，
    # 否则「没声明」会被伪造成「声明了并且正好匹配」）。
    want_scope = (query.scope or "").strip()
    report_scope = query.effective_scope
    dropped: dict[str, int] = {}
    notes: list[str] = []

    def drop(reason: str, count: int = 1) -> None:
        dropped[reason] = dropped.get(reason, 0) + count

    def scope_ok(item_scope: object) -> bool:
        if not want_scope:
            return True  # 宿主没声明 scope：不校验，但要记一笔
        return str(item_scope or "") == want_scope

    # ---- 阅读证据 ----
    readings: list[BiliReading] = []
    for entry in list(raw.get("readings") or [])[: max(0, query.max_readings * 3)]:
        if not isinstance(entry, dict):
            drop("reading_schema")
            continue
        picked = _pick(entry, READING_FIELDS)
        bvid = _as_str(picked.get("bvid"), 40)
        reading_id = _as_str(picked.get("reading_id"), 80)
        if not bvid or not reading_id:
            drop("reading_schema")
            continue
        if not scope_ok(picked.get("scope")):
            drop("reading_scope")
            continue
        evidence = _as_str(picked.get("evidence"), 40) or "metadata_only"
        subtitle = _as_str(picked.get("subtitle_state"), 20) or "unknown"
        coverage = _as_str(picked.get("coverage"), 20) or "none"
        if evidence not in EVIDENCE_ORDER or subtitle not in SUBTITLE_STATES or coverage not in COVERAGE:
            drop("reading_schema")
            continue
        readings.append(
            BiliReading(
                reading_id=reading_id,
                bvid=bvid,
                page=max(1, _as_int(picked.get("page"), 1)),
                cid=_as_str(picked.get("cid"), 40),
                revision=max(1, _as_int(picked.get("revision"), 1)),
                fetched_at=_as_str(picked.get("fetched_at"), 40),
                evidence=evidence,
                subtitle_state=subtitle,
                coverage=coverage,
                scope=_as_str(picked.get("scope"), 120),
            )
        )
        if len(readings) >= query.max_readings:
            break

    # ---- 有效认识 ----
    reflections: list[BiliReflection] = []
    newest = datetime.now(timezone.utc)
    for entry in list(raw.get("reflections") or [])[: max(0, query.max_reflections * 3)]:
        if not isinstance(entry, dict):
            drop("reflection_schema")
            continue
        picked = _pick(entry, REFLECTION_FIELDS)
        reflection_id = _as_str(picked.get("reflection_id"), 80)
        text = _as_str(picked.get("text"), 300)
        if not reflection_id or not text:
            drop("reflection_schema")
            continue
        state = _as_str(picked.get("state"), 20) or "active"
        if state != "active":
            drop(f"reflection_{state}")
            continue
        # 撤回/supersede 之后不许回填旧手账充数
        if _as_str(picked.get("source"), 20) == "journal":
            drop("reflection_journal_fallback")
            continue
        if not scope_ok(picked.get("scope")):
            drop("reflection_scope")
            continue
        stamped = _parse_dt(picked.get("updated_at"))
        if query.max_age_hours > 0 and stamped is not None:
            age_hours = (newest - stamped).total_seconds() / 3600.0
            if age_hours > query.max_age_hours:
                drop("reflection_stale")
                continue
        elif stamped is None and _as_str(picked.get("updated_at"), 40):
            drop("reflection_time_unparsable")
            continue
        reflections.append(
            BiliReflection(
                reflection_id=reflection_id,
                text=text,
                topic=_as_str(picked.get("topic"), 60),
                journal_id=_as_str(picked.get("journal_id"), 80),
                state=state,
                supersedes=_as_str(picked.get("supersedes"), 80),
                memory_id=_as_str(picked.get("memory_id"), 80),
                source_refs=_as_refs(picked.get("source_refs")),
                revision=max(1, _as_int(picked.get("revision"), 1)),
                scope=_as_str(picked.get("scope"), 120),
                updated_at=_as_str(picked.get("updated_at"), 40),
                source="reflection",
            )
        )
        if len(reflections) >= query.max_reflections:
            break

    # ---- 兴趣（只公开主题与权重） ----
    interests: list[BiliInterest] = []
    for entry in list(raw.get("interests") or [])[: max(0, query.max_interests * 2)]:
        if not isinstance(entry, dict):
            drop("interest_schema")
            continue
        picked = _pick(entry, INTEREST_FIELDS)
        topic = _as_str(picked.get("topic"), 60)
        if not topic:
            drop("interest_schema")
            continue
        interests.append(
            BiliInterest(
                topic=topic,
                weight=_as_float(picked.get("weight"), 0.0),
                updated_at=_as_str(picked.get("updated_at"), 40),
            )
        )
        if len(interests) >= query.max_interests:
            break

    # ---- 待批准提议（必须稳定 ID） ----
    proposals: list[BiliProposal] = []
    for entry in list(raw.get("proposals") or [])[: max(0, query.max_proposals * 2)]:
        if not isinstance(entry, dict):
            drop("proposal_schema")
            continue
        picked = _pick(entry, PROPOSAL_FIELDS)
        proposal_id = _as_str(picked.get("proposal_id"), 80)
        if not proposal_id:
            raise BiliContractError("提议缺少稳定 proposal_id：不接受「第 1 条」这种序号")
        if _ORDINAL.match(proposal_id):
            raise BiliContractError(f"提议 ID 是序号（{proposal_id}）：批准会打到错的那条，拒绝")
        if not scope_ok(picked.get("scope")):
            drop("proposal_scope")
            continue
        state = _as_str(picked.get("state"), 20) or "pending"
        if state not in PROPOSAL_STATES:
            drop("proposal_schema")
            continue
        proposals.append(
            BiliProposal(
                proposal_id=proposal_id,
                bvid=_as_str(picked.get("bvid"), 40),
                page=max(1, _as_int(picked.get("page"), 1)),
                state=state,
                revision=max(1, _as_int(picked.get("revision"), 1)),
                created_at=_as_str(picked.get("created_at"), 40),
                scope=_as_str(picked.get("scope"), 120),
            )
        )
        if len(proposals) >= query.max_proposals:
            break

    # ---- 运行摘要 ----
    raw_runtime = raw.get("runtime") if isinstance(raw.get("runtime"), dict) else {}
    picked_runtime = _pick(raw_runtime, RUNTIME_FIELDS)
    runtime = BiliRuntime(
        auto=_as_bool(picked_runtime.get("auto")),
        likes_enabled=_as_bool(picked_runtime.get("likes_enabled")),
        next_wake=_as_str(picked_runtime.get("next_wake"), 40),
        round=max(0, _as_int(picked_runtime.get("round"), 0)),
        likes_used=max(0, _as_int(picked_runtime.get("likes_used"), 0)),
        likes_limit=max(0, _as_int(picked_runtime.get("likes_limit"), 0)),
        shares_used=max(0, _as_int(picked_runtime.get("shares_used"), 0)),
        shares_limit=max(0, _as_int(picked_runtime.get("shares_limit"), 0)),
        asr_used=max(0, _as_int(picked_runtime.get("asr_used"), 0)),
        asr_limit=max(0, _as_int(picked_runtime.get("asr_limit"), 0)),
        asr_busy=_as_bool(picked_runtime.get("asr_busy")),
        asr_reserved=max(0, _as_int(picked_runtime.get("asr_reserved"), 0)),
        share_cooldown_minutes=max(0, _as_int(picked_runtime.get("share_cooldown_minutes"), 0)),
        quiet_start=max(0, _as_int(picked_runtime.get("quiet_start"), 0)),
        quiet_end=max(0, _as_int(picked_runtime.get("quiet_end"), 0)),
        last_error_type=error_kind(picked_runtime.get("last_error_type")) if picked_runtime.get("last_error_type") else "",
    )

    if not want_scope:
        notes.append("宿主没有声明 scope：本轮未做跨 scope 校验（仅按绑定关系过滤）")
    if dropped:
        notes.append("有 N 条材料没进上下文，原因：" + "、".join(sorted(dropped)))

    return BiliSnapshot(
        version=version,
        queried_at=_as_str(raw.get("queried_at"), 40),
        scope=_as_str(raw.get("scope"), 120) or report_scope,
        readings=tuple(readings),
        reflections=tuple(reflections),
        interests=tuple(interests),
        proposals=tuple(proposals),
        runtime=runtime,
        dropped=dropped,
        notes=tuple(notes),
    )


def empty_snapshot(reason: str, *, queried_at: str = "") -> BiliSnapshot:
    """桥不可用时的空快照：结构完整，但明确写着为什么是空的。"""
    return BiliSnapshot(queried_at=queried_at, notes=(reason,))
