"""恢复前算账（只读）：这样恢复会不会「复活已遗忘的话」「重发已发过的消息」。

M10-B 先把这段逻辑写在离线工具 `01_部署与运维/restore_check.py` 里；M11-B 把它下沉到插件：
`/陪伴备份 <名字>` 能当场算一遍，服务器上的 CLI 也 import 同一份实现——**只有一份账本算法**，
聊天里说的和命令行说的不会各说各话。

判定（这是这份代码存在的全部理由）：

1. **会重发**：现在的投递账本里有、备份里没有的 `idem_key`
   —— 恢复会把「已经发过」这件事忘掉，于是同一条会再发一次。
2. **会复活**：现在的记忆库里有、备份里没有的墓碑
   —— 恢复会把「已经忘掉」这件事忘掉，被遗忘的内容还能被重新记起来。
3. 更狠的一种：墓碑还在，但它记住的正文哈希能在备份的记录里命中。
4. **会丢 / 会带回**：备份与现在各有对方没有的记录（信息性，不是拦停理由）。

**只读**：SQLite 一律 `mode=ro` 打开，本模块不写、不删、不移动任何文件。
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

EXIT_SAFE = 0
EXIT_BAD = 1
EXIT_UNSAFE = 2

DELIVERY_FILES = ("deliveries.json", "delivery_ledger.json", "state/deliveries.json")
MEMORY_FILES = (
    "memory/feiyu_memory.db",
    "feiyu_memory.db",
    "memory.db",
)


# ---------------------------------------------------------------------------
# 找文件
# ---------------------------------------------------------------------------
def _find(root: Path, names) -> Path | None:
    for name in names:
        path = root / name
        if path.exists():
            return path
    return None


def _first_glob(root: Path, pattern: str) -> Path | None:
    """整目录备份里账本常被埋在 `plugin_data/<插件>/` 下，按模式找一遍。"""
    try:
        matches = sorted(root.glob(pattern))
    except (OSError, ValueError):
        return None
    for path in matches:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _first_existing(paths) -> Path | None:
    for path in paths or ():
        try:
            candidate = Path(path)
        except (TypeError, ValueError):
            continue
        if candidate.exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# 读账本
# ---------------------------------------------------------------------------
def _load_deliveries(path: Path | None) -> tuple[set[str], str]:
    """账本里的 idem_key 集合；文件不在或坏了都回 `(空, 说明)`。"""
    if path is None:
        return set(), "没有投递账本"
    try:
        raw = io.open(path, encoding="utf-8").read()
    except OSError as exc:
        return set(), f"账本读不了（{type(exc).__name__}）"
    if not raw.strip():
        return set(), "账本是空的"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return set(), "账本不是合法 JSON"
    if not isinstance(data, dict):
        return set(), "账本不是对象"
    return set(str(key) for key in data.keys()), f"{len(data)} 条"


# ---------------------------------------------------------------------------
# 读记忆库
# ---------------------------------------------------------------------------
def _open_ro(path: Path) -> sqlite3.Connection:
    """只读打开：`mode=ro` 下 SQLite 连日志文件都不会去写。"""
    uri = f"file:{Path(path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _memory_facts(path: Path | None) -> dict:
    """读出：记录 id、墓碑 (record_id, content_hash)、以及库里正文的哈希。"""
    empty = {
        "found": False,
        "records": set(),
        "tombstones": set(),
        "tombstone_hashes": set(),
        "content_hashes": set(),
        "note": "没有记忆库",
    }
    if path is None:
        return empty
    try:
        conn = _open_ro(path)
    except sqlite3.Error as exc:
        return {**empty, "note": f"记忆库打不开（{type(exc).__name__}）"}
    try:
        records: set[str] = set()
        content_hashes: set[str] = set()
        if _table_exists(conn, "memory_records"):
            for row in conn.execute("SELECT id, content FROM memory_records"):
                records.add(str(row["id"]))
                digest = hashlib.sha256(
                    str(row["content"] or "").encode("utf-8")
                ).hexdigest()[:32]
                content_hashes.add(digest)
        tombstones: set[str] = set()
        tombstone_hashes: set[str] = set()
        if _table_exists(conn, "memory_tombstones"):
            for row in conn.execute("SELECT record_id, content_hash FROM memory_tombstones"):
                tombstones.add(str(row["record_id"]))
                if row["content_hash"]:
                    tombstone_hashes.add(str(row["content_hash"]))
        return {
            "found": True,
            "records": records,
            "tombstones": tombstones,
            "tombstone_hashes": tombstone_hashes,
            "content_hashes": content_hashes,
            "note": f"记录 {len(records)} 条，墓碑 {len(tombstones)} 条",
        }
    except sqlite3.Error as exc:
        return {**empty, "note": f"记忆库读出错（{type(exc).__name__}）"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 算账
# ---------------------------------------------------------------------------
def audit(
    backup_dir,
    current_dir,
    *,
    current_memory_paths=(),
    current_ledger_paths=(),
) -> dict:
    """对「备份目录」与「现在的目录」算一遍。

    `current_memory_paths` / `current_ledger_paths` 是现网那几样东西的候选绝对路径：
    插件知道账本在 `plugin_data/<插件>/deliveries.json`、记忆库在 `data/feiyu/memory/` 下，
    CLI 不知道，所以由调用方补。备份那一侧除了按固定相对名找，还会往下看一眼
    `plugin_data/*/deliveries.json`（整目录备份就是这么放的）。
    """
    backup = Path(backup_dir)
    current = Path(current_dir)

    backup_ledger = _find(backup, DELIVERY_FILES) or _first_glob(
        backup, "plugin_data/*/deliveries.json"
    )
    current_ledger = (
        _find(current, DELIVERY_FILES)
        or _first_glob(current, "plugin_data/*/deliveries.json")
        or _first_existing(current_ledger_paths)
    )
    backup_keys, backup_note = _load_deliveries(backup_ledger)
    current_keys, current_note = _load_deliveries(current_ledger)

    backup_mem_path = _find(backup, MEMORY_FILES)
    if backup_mem_path is None:
        for path in current_memory_paths or ():
            candidate = backup / "memory" / Path(path).name
            if candidate.exists():
                backup_mem_path = candidate
                break
    current_mem_path = _find(current, MEMORY_FILES) or _first_existing(current_memory_paths)

    backup_mem = _memory_facts(backup_mem_path)
    current_mem = _memory_facts(current_mem_path)

    would_resend = sorted(current_keys - backup_keys)
    backup_only_keys = sorted(backup_keys - current_keys)
    revive_tombstones = sorted(current_mem["tombstones"] - backup_mem["tombstones"])
    revive_hashes = sorted(current_mem["tombstone_hashes"] & backup_mem["content_hashes"])
    lost_records = sorted(current_mem["records"] - backup_mem["records"])
    revived_records = sorted(backup_mem["records"] - current_mem["records"])

    unsafe = bool(would_resend or revive_tombstones or revive_hashes)
    return {
        "backup_dir": str(backup),
        "current_dir": str(current),
        "deliveries": {
            "backup_file": str(backup_ledger) if backup_ledger else "",
            "current_file": str(current_ledger) if current_ledger else "",
            "backup_note": backup_note,
            "current_note": current_note,
            "would_resend": would_resend,
            "backup_only": backup_only_keys,
        },
        "memory": {
            "backup_file": str(backup_mem_path) if backup_mem_path else "",
            "current_file": str(current_mem_path) if current_mem_path else "",
            "backup_note": backup_mem["note"],
            "current_note": current_mem["note"],
            "revive_tombstones": revive_tombstones,
            "revive_hashes": revive_hashes,
            "lost_records": lost_records,
            "revived_records": revived_records,
        },
        "verdict": "unsafe" if unsafe else "safe",
        "exit": EXIT_UNSAFE if unsafe else EXIT_SAFE,
    }


def report_lines(result: dict) -> list[str]:
    """人话版算账报告（命令行与聊天共用）。"""
    deliveries = result["deliveries"]
    memory = result["memory"]
    lines = [
        f"备份：{result['backup_dir']}",
        f"现在：{result['current_dir']}",
        "",
        f"投递账本：备份 {deliveries['backup_note']}；现在 {deliveries['current_note']}",
    ]
    if deliveries["would_resend"]:
        lines.append(
            f"  ✗ 会重发 {len(deliveries['would_resend'])} 条："
            + "、".join(deliveries["would_resend"][:5])
            + ("…" if len(deliveries["would_resend"]) > 5 else "")
        )
    else:
        lines.append("  ✓ 不会重发：现在账本里的每一条，备份里都记着")
    if deliveries["backup_only"]:
        lines.append(
            f"  · 备份里多出 {len(deliveries['backup_only'])} 条旧账"
            "（恢复后会多这么几行，不会真发）"
        )

    lines.append("")
    lines.append(f"记忆库：备份 {memory['backup_note']}；现在 {memory['current_note']}")
    if memory["revive_tombstones"]:
        lines.append(
            f"  ✗ 会复活 {len(memory['revive_tombstones'])} 条被遗忘的记录："
            + "、".join(memory["revive_tombstones"][:5])
            + ("…" if len(memory["revive_tombstones"]) > 5 else "")
        )
    else:
        lines.append("  ✓ 不会复活：现在的墓碑，备份里也都有")
    if memory["revive_hashes"]:
        lines.append(
            f"  ✗ 有 {len(memory['revive_hashes'])} 处墓碑的正文哈希能在备份里找到对应记录"
        )
    if memory["lost_records"]:
        lines.append(f"  · 恢复会丢掉现在才有的 {len(memory['lost_records'])} 条记录（信息性）")
    if memory["revived_records"]:
        lines.append(
            f"  · 恢复会带回备份里才有的 {len(memory['revived_records'])} 条记录（信息性）"
        )

    lines.append("")
    if result["verdict"] == "unsafe":
        lines.append(
            "结论：**不要直接覆盖恢复**。先做两件事：把现在的墓碑并进备份"
            "（或直接保留现在的墓碑表），再把现在的投递账本并进备份。"
            "并完再算一遍，看到两个 ✓ 才动手。"
        )
    else:
        lines.append(
            "结论：这一份备份可以直接恢复——不会复活已遗忘的话，也不会重发已发过的消息。"
        )
    return lines


def summarize(result: dict, *, limit: int = 3) -> str:
    """一句话结论（聊天里用），不展开清单。"""
    if result.get("verdict") == "unsafe":
        rs = result["deliveries"]["would_resend"]
        rv = result["memory"]["revive_tombstones"]
        parts = []
        if rs:
            parts.append(f"会重发 {len(rs)} 条（{'、'.join(rs[:limit])}{'…' if len(rs) > limit else ''}）")
        if rv:
            parts.append(f"会复活 {len(rv)} 条（{'、'.join(rv[:limit])}{'…' if len(rv) > limit else ''}）")
        if result["memory"]["revive_hashes"]:
            parts.append(f"{len(result['memory']['revive_hashes'])} 处墓碑正文能在备份里找到")
        return "不要直接恢复：" + "；".join(parts)
    return "可以直接恢复：不会重发，也不会复活已遗忘的内容"


__all__ = [
    "DELIVERY_FILES",
    "MEMORY_FILES",
    "EXIT_SAFE",
    "EXIT_BAD",
    "EXIT_UNSAFE",
    "audit",
    "report_lines",
    "summarize",
]
