"""把「这一刻线上是什么样」落成一份 JSON 快照。

只做三件事：把给定的事实整理成一个 dict、写进自己的目录、把旧的多余的删掉。
不读记忆库、不碰配置、不发消息；写文件只用 tmp + `os.replace`，删的也只可能是
本目录里自己写出来的 `*.json`。

为什么要它：`/陪伴状态` 是「现在」，日志是「流水」，而「那天线上到底是什么配置、
哪几项开着、额度用了多少」在事后很难还原。快照就是给未来的自己留一份当时的像。
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = [
    "SNAPSHOT_VERSION",
    "DEFAULT_KEEP",
    "SENSITIVE_HINTS",
    "safe_config",
    "stamp_of",
    "build",
    "write",
    "list_snapshots",
    "describe",
]

SNAPSHOT_VERSION = 1
DEFAULT_KEEP = 50
MAX_TEXT = 120
TZ = timezone(timedelta(hours=8))

#: 这些字样出现在键名里就当敏感：只留长度，不留值。
SENSITIVE_HINTS = ("token", "secret", "password", "passwd", "api_key", "apikey", "cookie")


def _short(text, limit: int = MAX_TEXT) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def safe_config(config: dict | None, *, limit: int = 400) -> dict:
    """配置的安全投影：敏感键只留长度，其余原样（布尔/数字/短字符串）。

    只取前 `limit` 个键，避免有人把整份配置塞进来撑爆快照。
    """
    out: dict = {}
    for index, key in enumerate(sorted(dict(config or {}))):
        if index >= limit:
            out["…"] = f"还有更多键，未全部记录"
            break
        name = str(key)
        try:
            value = config[key]
        except (KeyError, TypeError):
            continue
        lowered = name.lower()
        if any(hint in lowered for hint in SENSITIVE_HINTS):
            out[name] = f"〔{len(str(value or ''))} 字符〕"
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            out[name] = value
        elif isinstance(value, (list, tuple)):
            out[name] = f"〔列表 {len(value)} 项〕"
        elif isinstance(value, dict):
            out[name] = f"〔对象 {len(value)} 键〕"
        else:
            out[name] = _short(value, 80)
    return out


def _to_local(moment) -> datetime:
    if isinstance(moment, datetime):
        return moment.astimezone(TZ) if moment.tzinfo else moment.replace(tzinfo=TZ)
    return datetime.now(TZ)


def stamp_of(moment=None) -> str:
    """`20261026-210512`：文件名用，排序即时间序。"""
    return _to_local(moment).strftime("%Y%m%d-%H%M%S")


def build(
    *,
    version: str,
    config: dict | None = None,
    capabilities: list | None = None,
    usage: dict | None = None,
    ledger: dict | None = None,
    fingerprint: str = "",
    moment=None,
    extra: dict | None = None,
) -> dict:
    """拼一份快照。`capabilities` 是 `[{capability, label, state, quota, note}, …]`。"""
    local = _to_local(moment)
    rows = []
    for row in capabilities or ():
        item = dict(row or {})
        rows.append(
            {
                "capability": _short(item.get("capability"), 40),
                "label": _short(item.get("label"), 24),
                "state": _short(item.get("state"), 24),
                "quota": _short(item.get("quota"), 40),
                "note": _short(item.get("note"), 80),
            }
        )
    data = {
        "snapshot_version": SNAPSHOT_VERSION,
        "taken_at": local.isoformat(timespec="seconds"),
        "stamp": stamp_of(local),
        "version": _short(version, 40),
        "fingerprint": _short(fingerprint, 24),
        "config": safe_config(config),
        "capabilities": rows,
        "usage": {str(k): v for k, v in dict(usage or {}).items()},
        "ledger": dict(ledger or {}),
    }
    if extra:
        data["extra"] = {str(k): v for k, v in dict(extra).items()}
    return data


def _unique_path(directory: Path, stamp: str) -> Path:
    candidate = directory / f"{stamp}.json"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stamp}-{index}.json"
        index += 1
    return candidate


def write(snapshot_dir, data: dict, *, keep: int = DEFAULT_KEEP) -> Path:
    """写一份快照，并只保留最近的 `keep` 份（按文件名排序＝时间序）。"""
    directory = Path(snapshot_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = str(data.get("stamp") or stamp_of())
    target = _unique_path(directory, stamp)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(prefix=".snap-", suffix=".tmp", dir=str(directory))
    try:
        with io.open(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _prune(directory, keep)
    return target


def _prune(directory: Path, keep: int) -> None:
    try:
        files = sorted(path for path in directory.glob("*.json") if path.is_file())
    except OSError:
        return
    limit = max(1, int(keep))
    for path in files[:-limit]:
        try:
            path.unlink()
        except OSError:
            continue


def list_snapshots(snapshot_dir) -> list[dict]:
    """从新到旧列出来（只读文件头几行不需要的东西不解析，直接读整份）。"""
    directory = Path(snapshot_dir)
    out: list[dict] = []
    try:
        files = sorted((p for p in directory.glob("*.json") if p.is_file()), reverse=True)
    except OSError:
        return out
    for path in files:
        try:
            data = json.loads(io.open(path, encoding="utf-8").read())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        out.append(
            {
                "name": path.name,
                "taken_at": _short(data.get("taken_at"), 40),
                "version": _short(data.get("version"), 40),
                "bytes": path.stat().st_size,
            }
        )
    return out


def describe(data: dict, *, limit: int = 6) -> list[str]:
    """人话：这一份快照里有什么。不回显任何敏感值。"""
    rows = list(data.get("capabilities") or ())
    on = [row for row in rows if row.get("state") in ("on", "delegated")]
    lines = [
        f"快照（{data.get('taken_at') or '?'}，插件 v{data.get('version') or '?'}）",
        f"能力 {len(rows)} 项，其中在用 {len(on)} 项；配置指纹 {data.get('fingerprint') or '（无）'}",
    ]
    for row in on[:limit]:
        quota = f"（{row.get('quota')}）" if row.get("quota") else ""
        lines.append(f"· {row.get('label')}：{row.get('state')}{quota}")
    ledger = dict(data.get("ledger") or {})
    if ledger:
        bits = "、".join(f"{key} {value}" for key, value in list(ledger.items())[:4])
        lines.append(f"账本：{bits}")
    return lines
