"""备份清单与配置 revision 的纯逻辑（M10-B）。

这里**不碰磁盘**：文件系统由 service 层注入（列出目录、读账本），
本文件只负责把「一堆备份文件」与「两次配置」变成能直接说给人看的结构。

设计取舍：
- 备份名里就带时间戳（部署脚本一直这么起名），所以时间从名字里解析，
  不依赖文件 mtime（scp 之后 mtime 会变，名字不会）。
- 配置对比**只显示「变了」和「变成什么形状」**：开关显示开/关、数字显示数字、
  字符串只显示「有值/空」与长度，绝不把令牌原样打出来；再用 6 位摘要对账。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

BACKUP_PORT_NAME = "BackupPortV1"
BACKUP_VERSION = 1

#: 一次最多列这么多条，免得备份攒了几百份把回话撑爆。
MAX_ENTRIES = 50
#: 配置 revision 账本最多留这么多条。
MAX_REVISIONS = 200

KIND_CONFIG = "config"
KIND_PLUGIN = "plugin"
KIND_DATA = "data"
KIND_UNKNOWN = "unknown"
KIND_LABELS = {
    KIND_CONFIG: "配置",
    KIND_PLUGIN: "插件目录",
    KIND_DATA: "数据",
    KIND_UNKNOWN: "说不清",
}

_STAMP_RE = re.compile(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$")


class BackupError(ValueError):
    """备份清单里的东西说不通。"""


# ---------------------------------------------------------------- 名字与时间
def parse_stamp(name: str) -> str:
    """从备份名解析出「2026-10-26 19:47:42」；解析不出就回空串。"""
    match = _STAMP_RE.search(str(name or ""))
    if not match:
        return ""
    year, month, day, hour, minute, second = match.groups()
    return f"{year}-{month}-{day} {hour}:{minute}:{second}"


def kind_of(name: str) -> str:
    """按名字猜这份备份是什么。猜错也不要紧，显示时只是少一个标签。"""
    text = str(name or "").lower()
    if text.endswith(".json") or "config" in text:
        return KIND_CONFIG
    if "astrbot_plugin_" in text or "plugin" in text:
        return KIND_PLUGIN
    if any(word in text for word in ("data", "feiyu", "_backups", "state")):
        return KIND_DATA
    return KIND_UNKNOWN


def human_size(size: int) -> str:
    try:
        value = int(size)
    except (TypeError, ValueError):
        return "?"
    if value < 0:
        return "?"
    for unit, limit in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if value >= limit:
            return f"{value / limit:.1f} {unit}"
    return f"{value} B"


# ---------------------------------------------------------------- 清单条目
@dataclass
class BackupEntry:
    """一份备份。`sha256` 可以空（没算过），显示时就少一截。"""

    name: str
    size: int = 0
    sha256: str = ""
    kind: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise BackupError("备份没有名字")
        if not self.kind:
            self.kind = kind_of(self.name)

    @property
    def stamp(self) -> str:
        return parse_stamp(self.name)

    @property
    def short_hash(self) -> str:
        return (self.sha256 or "")[:12]

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "stamp": self.stamp,
            "kind": self.kind,
            "size": int(self.size),
            "size_text": human_size(self.size),
            "sha256_12": self.short_hash,
            "note": self.note,
        }


@dataclass
class BackupManifest:
    """一次扫描的结果：按时间从新到旧排。"""

    root: str = ""
    entries: list[BackupEntry] = field(default_factory=list)
    scanned_at: str = ""
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.entries = sorted(
            self.entries, key=lambda item: (item.stamp, item.name), reverse=True
        )[:MAX_ENTRIES]

    @property
    def latest(self) -> BackupEntry | None:
        return self.entries[0] if self.entries else None

    def counts(self) -> dict:
        out = {"total": len(self.entries)}
        for kind in (KIND_CONFIG, KIND_PLUGIN, KIND_DATA, KIND_UNKNOWN):
            out[kind] = sum(1 for entry in self.entries if entry.kind == kind)
        return out

    def to_dict(self) -> dict:
        return {
            "version": BACKUP_VERSION,
            "root": self.root,
            "scanned_at": self.scanned_at,
            "counts": self.counts(),
            "entries": [entry.to_dict() for entry in self.entries],
            "notes": list(self.notes),
        }


def scan(rows: list[dict], *, root: str = "", scanned_at: str = "") -> BackupManifest:
    """`rows` 是 service 层从磁盘读到的 `{name, size, sha256}`，这里只做整理。"""
    entries = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        entries.append(
            BackupEntry(
                name=name,
                size=int(row.get("size") or 0),
                sha256=str(row.get("sha256") or ""),
                kind=str(row.get("kind") or ""),
                note=str(row.get("note") or ""),
            )
        )
    return BackupManifest(root=root, entries=entries, scanned_at=scanned_at)


def describe(manifest: BackupManifest, *, limit: int = 10) -> list[str]:
    """把清单说成人话：一行一份，最多 `limit` 行。"""
    if manifest is None:
        return ["还没扫过备份。"]
    counts = manifest.counts()
    if not manifest.entries:
        where = f"（{manifest.root}）" if manifest.root else ""
        return [f"没有找到备份{where}。"]
    lines = [
        f"备份 {counts['total']} 份（配置 {counts[KIND_CONFIG]}、插件 {counts[KIND_PLUGIN]}、"
        f"数据 {counts[KIND_DATA]}" + (f"、说不清 {counts[KIND_UNKNOWN]}" if counts[KIND_UNKNOWN] else "") + "）"
    ]
    if manifest.root:
        lines.append(f"目录：{manifest.root}")
    for entry in manifest.entries[: max(1, int(limit))]:
        stamp = entry.stamp or "（名字里没有时间）"
        bits = [stamp, entry.label, human_size(entry.size)]
        if entry.short_hash:
            bits.append(f"#{entry.short_hash}")
        line = "· " + "  ".join(bits) + f"  {entry.name}"
        if entry.note:
            line += f"（{entry.note}）"
        lines.append(line)
    rest = len(manifest.entries) - min(len(manifest.entries), max(1, int(limit)))
    if rest > 0:
        lines.append(f"…… 还有 {rest} 份没列。")
    lines.extend(manifest.notes)
    return lines


# ---------------------------------------------------------------- 配置对比
def _digest_value(value) -> str:
    blob = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:6]


def value_shape(value) -> str:
    """只描述值的形状，不吐值本身。"""
    if isinstance(value, bool):
        return "开" if value else "关"
    if value is None:
        return "没设"
    if isinstance(value, (int, float)):
        return f"数字 {value}"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "空"
        return f"文字 {len(text)} 字"
    if isinstance(value, (list, tuple)):
        return f"列表 {len(value)} 项"
    if isinstance(value, dict):
        return f"对象 {len(value)} 键"
    return type(value).__name__


def digest_map(config: dict, keys: list[str] | None = None) -> dict:
    """给每个键算 6 位摘要：下次启动只靠摘要就能看出「哪个键变了」，不用存值。"""
    data = dict(config or {})
    if keys:
        data = {key: data.get(key) for key in keys}
    return {key: _digest_value(data.get(key)) for key in sorted(data)}


def diff_digests(previous: dict, current: dict) -> list[str]:
    """两个摘要表之间变了哪些键（新增/删除/改值都算）。"""
    prev = dict(previous or {})
    cur = dict(current or {})
    keys = set(prev) | set(cur)
    return sorted(key for key in keys if prev.get(key) != cur.get(key))


def compare_config(old: dict, new: dict, *, limit: int = 20) -> list[tuple[str, str, str]]:
    """两份配置的差异：`(键, 旧形状, 新形状)`。只给形状，不给值。"""
    old = dict(old or {})
    new = dict(new or {})
    rows = []
    for key in sorted(set(old) | set(new)):
        before = value_shape(old.get(key))
        after = value_shape(new.get(key))
        if before != after:
            rows.append((key, before, after))
    return rows[: max(1, int(limit))]


def diff_lines(old: dict, new: dict, *, limit: int = 20) -> list[str]:
    rows = compare_config(old, new, limit=limit)
    if not rows:
        return ["两份配置在形状上没有差别。"]
    lines = [f"配置差别 {len(rows)} 处："]
    for key, before, after in rows:
        lines.append(f"· {key}：{before} → {after}")
    return lines


# ---------------------------------------------------------------- revision 账本
def revision_record(
    fingerprint: str,
    digests: dict,
    *,
    at: str,
    changed: list[str] | None = None,
    note: str = "",
) -> dict:
    """一条配置 revision：时间 + 指纹 + 每个键的 6 位摘要 + 这次变了哪些键。"""
    return {
        "at": str(at or ""),
        "fingerprint": str(fingerprint or ""),
        "digests": dict(digests or {}),
        "changed": [str(key) for key in (changed or [])],
        "note": str(note or ""),
    }


def load_revisions(text: str) -> list[dict]:
    """把一个 jsonl 账本读成列表；坏行跳过，不因为一行坏掉整本账。"""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("fingerprint"):
            out.append(row)
    return out


def needs_revision(revisions: list[dict], fingerprint: str) -> bool:
    """最近一条的指纹与现在不同才需要记一条。"""
    if not revisions:
        return True
    return str(revisions[-1].get("fingerprint") or "") != str(fingerprint or "")


def revision_lines(revisions: list[dict], *, limit: int = 5) -> list[str]:
    if not revisions:
        return ["配置 revision：还没有记录（第一次启动后就有了）。"]
    shown = revisions[-max(1, int(limit)) :]
    lines = [f"配置 revision 共 {len(revisions)} 条，最近 {len(shown)} 条："]
    for row in reversed(shown):
        changed = row.get("changed")
        bits = [str(row.get("at") or "（没有时间）"), f"指纹 {str(row.get('fingerprint') or '')[:12]}"]
        if changed:
            bits.append("变了 " + "、".join(str(key) for key in list(changed)[:6]))
        else:
            bits.append("首次记录")
        lines.append("· " + "  ".join(bits))
    return lines


__all__ = [
    "BACKUP_PORT_NAME",
    "BACKUP_VERSION",
    "MAX_ENTRIES",
    "MAX_REVISIONS",
    "KIND_CONFIG",
    "KIND_PLUGIN",
    "KIND_DATA",
    "KIND_UNKNOWN",
    "KIND_LABELS",
    "BackupError",
    "BackupEntry",
    "BackupManifest",
    "parse_stamp",
    "kind_of",
    "human_size",
    "scan",
    "describe",
    "value_shape",
    "digest_map",
    "diff_digests",
    "compare_config",
    "diff_lines",
    "revision_record",
    "load_revisions",
    "needs_revision",
    "revision_lines",
]
