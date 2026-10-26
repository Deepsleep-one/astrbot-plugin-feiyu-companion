"""备份清单 + 配置 revision + 恢复前算账的只读一回合（M10-B / M11-B）。

`/陪伴备份` 走这里。它做四件事，全是**只读**（唯一会写的是自己的 revision 账本）：

1. 列出现在有哪些备份：名字、时间、大小、类型、短哈希（只列，不删不改不恢复）；
2. 说清现在线上的配置指纹，并把「这一次启动相对上一次配置变了哪几个键」记进
   `state/config_revision.jsonl`（只记键名与 6 位摘要，**不记值**）；
3. 说清「相对上一次记录，配置变了几个键」（`/陪伴状态` 也用它）；
4. `/陪伴备份 <名字>` 时当场算一遍账：这样恢复会不会复活已遗忘的话、会不会重发
   已发过的消息（算法与 `01_部署与运维/restore_check.py` 是**同一份**
   `services/restore_audit.py`，两边不会各说各话）。算完只告诉你结论，**不替你恢复**。

如果备份目录不存在或读不了，如实说，不假装「没有备份」就是「备份都好好的」。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ..domain.context import InteractionContext
from ..domain.grants import CAP_TEXT, PermissionPolicy
from ..ports.backup import (
    MAX_ENTRIES,
    MAX_REVISIONS,
    BackupManifest,
    describe,
    diff_digests,
    digest_map,
    human_size,
    load_revisions,
    needs_revision,
    revision_lines,
    revision_record,
    scan,
)
from ..ports.workbench import config_fingerprint, redact
from .restore_audit import audit as audit_dirs
from .restore_audit import report_lines as audit_lines
from .restore_audit import summarize as audit_summary

TZ = timezone(timedelta(hours=8))
DEFAULT_LIMIT = 10
#: 只对不太大的文件算哈希，免得一次扫几十个几百 MB 的目录把事件循环卡住。
HASH_LIMIT_BYTES = 8 * 1024 * 1024
REVISION_FILE = "config_revision.jsonl"


def _now() -> datetime:
    return datetime.now(TZ)


@dataclass
class BackupAudit:
    """对某一份备份算完的账（只读结论，不是一个恢复动作）。"""

    ok: bool
    reason: str = ""
    target: str = ""
    path: str = ""
    result: dict = None  # type: ignore[assignment]
    verdict: str = ""

    @property
    def unsafe(self) -> bool:
        return self.verdict == "unsafe"

    def summary(self) -> str:
        if not self.ok or not isinstance(self.result, dict):
            return self.reason or "这份备份算不出账"
        return audit_summary(self.result)


@dataclass
class BackupPlan:
    ok: bool
    reason: str = ""
    manifest: BackupManifest | None = None
    revisions: list[dict] = None  # type: ignore[assignment]
    fingerprint: str = ""
    grant: object = None
    target: str = ""
    audit: BackupAudit | None = None
    changed: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.revisions is None:
            self.revisions = []
        if self.changed is None:
            self.changed = []


@dataclass
class BackupOutcome:
    ok: bool
    reason: str = ""
    text: str = ""

    @property
    def needs_fallback(self) -> bool:
        return not self.ok


class BackupTurn:
    """列备份、记 revision。不删、不改、不恢复。"""

    def __init__(
        self,
        policy: PermissionPolicy,
        *,
        state_dir: str | Path,
        backup_dir: str | Path = "",
        config: dict | None = None,
        limit: int = DEFAULT_LIMIT,
        clock=None,
        lister=None,
    ) -> None:
        self.policy = policy
        self.state_dir = Path(state_dir)
        self.config = dict(config or {})
        self.limit = max(1, min(int(limit), MAX_ENTRIES))
        self.clock = clock
        self._lister = lister
        self.last_error = ""
        self.calls = 0
        self.configured_dir = str(backup_dir or "").strip()
        self.backup_dirs = (
            [Path(self.configured_dir)] if self.configured_dir else self._default_dirs()
        )

    # ---- 基础 -------------------------------------------------------
    def _now(self) -> datetime:
        if self.clock is not None and hasattr(self.clock, "now"):
            return self.clock.now()
        return _now()

    def _default_dirs(self) -> list[Path]:
        """默认看两处：部署脚本一直往这两处放备份。

        - `<state_dir>/../_backups`：数据备份（`data/feiyu/_backups/`）
        - `<state_dir>/../../plugins/.plugin_backups`：插件目录备份
        """
        out: list[Path] = []
        try:
            out.append(self.state_dir.parent / "_backups")
            out.append(self.state_dir.parent.parent / "plugins" / ".plugin_backups")
        except Exception:  # noqa: BLE001
            out.append(self.state_dir / "_backups")
        return out

    @property
    def backup_dir(self) -> Path:
        """第一个候选目录（状态行与提示用它）。"""
        return self.backup_dirs[0]

    @property
    def revision_path(self) -> Path:
        return self.state_dir / REVISION_FILE

    def _gate(self, ctx: InteractionContext) -> str:
        if not ctx.is_private:
            return "备份清单只在私聊里看：路径和名字不该念给群里"
        if not ctx.is_owner:
            return "备份清单只给主人看"
        if not ctx.is_bound:
            return "还没认人：先在私聊里绑定，我才知道对面是你"
        return ""

    # ---- 列目录 -----------------------------------------------------
    def _hash_file(self, path: Path) -> str:
        try:
            if path.stat().st_size > HASH_LIMIT_BYTES:
                return ""
            digest = hashlib.sha256()
            with io.open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return ""

    def _dir_size(self, root: Path) -> tuple[int, int]:
        """目录备份的总大小与文件数（走一遍，不读内容；超过 2 万个文件就停）。"""
        total = 0
        count = 0
        stack = [root]
        while stack and count < 20000:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                                continue
                            total += entry.stat(follow_symlinks=False).st_size
                            count += 1
                        except OSError:
                            continue
            except OSError:
                continue
        return total, count

    def _scan_dir(self, root: Path) -> tuple[list[dict], str]:
        """读一个目录，回 `(行, 说明)`；读不到就把原因带回去。

        文件和**目录**都算一份备份：部署脚本实际留下的就是整目录备份
        （`astrbot_plugin_xxx-<时间戳>/`、`pre-m6d-<时间戳>/`），只列文件会漏掉它们，
        连「算一份备份的账」都无从下手。
        """
        if not root.exists():
            return [], f"备份目录还不存在：{root}"
        if not root.is_dir():
            return [], f"备份路径不是目录：{root}"
        rows = []
        notes = ""
        try:
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_file = not is_dir and entry.is_file(follow_symlinks=False)
                    except OSError:
                        continue
                    if not (is_dir or is_file):
                        continue
                    if is_dir:
                        size, count = self._dir_size(Path(entry.path))
                        rows.append({"name": entry.name, "size": size, "sha256": "", "note": f"{count} 个文件"})
                        continue
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    rows.append(
                        {
                            "name": entry.name,
                            "size": size,
                            "sha256": self._hash_file(Path(entry.path)),
                        }
                    )
        except OSError as exc:
            return [], f"备份目录读不了（{type(exc).__name__}）"
        if len(rows) > MAX_ENTRIES:
            notes = f"目录里有很多文件，只列最新的 {MAX_ENTRIES} 份。"
        return rows, notes

    def manifest(self) -> BackupManifest:
        rows: list[dict] = []
        notes: list[str] = []
        seen: set[str] = set()
        for root in self.backup_dirs:
            if callable(self._lister):
                try:
                    part, note = self._lister(root)
                except TypeError:
                    part, note = self._lister()
            else:
                part, note = self._scan_dir(root)
            for row in part or []:
                row = dict(row)
                name = str(row.get("name") or "")
                if name and name in seen:
                    continue  # 两个目录里同名只算一份
                if name:
                    seen.add(name)
                row.setdefault("note", root.name)
                rows.append(row)
            if note and str(note) not in notes:
                notes.append(str(note))
        manifest = scan(
            rows,
            root="；".join(str(root) for root in self.backup_dirs),
            scanned_at=self._now().isoformat(timespec="seconds"),
        )
        manifest.notes.extend(notes)
        return manifest

    # ---- 配置 revision ----------------------------------------------
    def _read_revisions(self) -> list[dict]:
        try:
            text = self.revision_path.read_text(encoding="utf-8") if self.revision_path.exists() else ""
        except OSError as exc:
            self.last_error = f"revision 读不了：{type(exc).__name__}"
            return []
        return load_revisions(text)

    def notice_revision(self, *, write: bool = True) -> list[dict]:
        """这次启动相对上一次，配置变了没。变了就记一条（只记键名与摘要）。"""
        fingerprint = config_fingerprint(self.config)
        digests = digest_map(self.config)
        revisions = self._read_revisions()
        if not needs_revision(revisions, fingerprint):
            return revisions
        changed = diff_digests(revisions[-1].get("digests") if revisions else {}, digests) if revisions else []
        record = revision_record(
            fingerprint,
            digests,
            at=self._now().isoformat(timespec="seconds"),
            changed=changed,
            note="插件启动时自动记录",
        )
        revisions = (revisions + [record])[-MAX_REVISIONS:]
        if write:
            self._append(record, revisions)
        return revisions

    def _append(self, record: dict, keep: list[dict]) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            lines = [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in keep]
            tmp = self.revision_path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, self.revision_path)
        except OSError as exc:
            self.last_error = f"revision 写不了：{type(exc).__name__}"

    def changed_since_last(self) -> list[str]:
        """相对上一次记下的 revision，配置变了哪几个键（没变就是空）。"""
        revisions = self._read_revisions()
        if not revisions:
            return []
        return diff_digests(revisions[-1].get("digests") or {}, digest_map(self.config))

    # ---- 恢复前算账（只读） ------------------------------------------
    def _memory_paths(self) -> list[Path]:
        """现网记忆库的候选路径（部署布局：`data/feiyu/memory/feiyu_memory.db`）。"""
        out: list[Path] = []
        try:
            out.append(self.state_dir.parent.parent / "feiyu" / "memory" / "feiyu_memory.db")
            out.append(self.state_dir.parent.parent / "feiyu" / "feiyu_memory.db")
            out.append(self.state_dir.parent / "memory" / "feiyu_memory.db")
        except Exception:  # noqa: BLE001
            return out
        return out

    def _ledger_paths(self) -> list[Path]:
        """现网投递账本的候选路径（`plugin_data/<插件>/deliveries.json`）。"""
        out: list[Path] = []
        try:
            out.append(self.state_dir / "deliveries.json")
            out.append(self.state_dir.parent.parent / "plugin_data" / "astrbot_plugin_feiyu_companion" / "deliveries.json")
        except Exception:  # noqa: BLE001
            return out
        return out

    def resolve(self, target: str) -> tuple[object | None, str]:
        """按名字找一份备份：先精确，再前缀，再包含；找不准就说清楚。"""
        want = str(target or "").strip()
        if not want:
            return None, "要说清楚算哪一份：`/陪伴备份 <名字>`"
        entries = list(self.manifest().entries)
        exact = [row for row in entries if row.name == want]
        if len(exact) == 1:
            return exact[0], ""
        prefix = [row for row in entries if row.name.startswith(want)]
        if len(prefix) == 1:
            return prefix[0], ""
        contains = [row for row in entries if want in row.name]
        if len(contains) == 1:
            return contains[0], ""
        if len(contains) > 1:
            names = "、".join(row.name for row in contains[:3])
            return None, f"有 {len(contains)} 份对得上「{want}」：{names}——说全一点"
        if not entries:
            return None, "现在没有能算的备份（备份目录里是空的）"
        names = "、".join(row.name for row in entries[:3])
        return None, f"没找到叫「{want}」的备份。现在有的是：{names}"

    def audit(self, target: str, *, current_dir: str | Path = "") -> BackupAudit:
        """对一份备份算账：会不会重发、会不会复活。只读，不动任何文件。"""
        entry, why = self.resolve(target)
        if entry is None:
            return BackupAudit(False, why, target=str(target))
        found = ""
        for root in self.backup_dirs:
            candidate = root / entry.name
            try:
                exists = candidate.exists()
            except OSError:
                exists = False
            if exists:
                found = str(candidate)
                break
        if not found:
            return BackupAudit(False, "这一份备份不在原来的目录里了", target=entry.name)
        current = Path(current_dir) if current_dir else self.state_dir
        try:
            result = audit_dirs(
                found,
                current,
                current_memory_paths=self._memory_paths(),
                current_ledger_paths=self._ledger_paths(),
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"算账失败：{type(exc).__name__}"
            return BackupAudit(False, f"算不动（{type(exc).__name__}）", target=entry.name, path=found)
        # 整目录/配置备份里既没有账本也没有记忆库：算不出「会不会重发」，如实说。
        nothing = (
            result["deliveries"]["backup_note"] == "没有投递账本"
            and result["memory"]["backup_note"] == "没有记忆库"
        )
        if nothing:
            reason = (
                "这份备份里没有投递账本也没有记忆库：它是整目录/配置备份，"
                "算不出「会不会重发、会不会复活」。要算账请对着数据备份算。"
            )
            return BackupAudit(False, reason, target=entry.name, path=found,
                               result=result, verdict=result["verdict"])
        return BackupAudit(
            True,
            target=entry.name,
            path=found,
            result=result,
            verdict=str(result.get("verdict") or ""),
        )

    # ---- 一回合 -----------------------------------------------------
    def build(self, ctx: InteractionContext, *, write: bool = False, target: str = "") -> BackupPlan:
        why = self._gate(ctx)
        if why:
            return BackupPlan(False, why)
        manifest = self.manifest()
        revisions = self.notice_revision(write=write)
        changed = self.changed_since_last()
        report = self.audit(target) if str(target or "").strip() else None
        try:
            grant = self.policy.grant_once(
                CAP_TEXT, scene=ctx.scene, reason="备份清单只看名字"
            )
        except Exception as exc:  # noqa: BLE001
            grant = None
            self.last_error = type(exc).__name__
        self.calls += 1
        return BackupPlan(
            True,
            manifest=manifest,
            revisions=revisions,
            fingerprint=config_fingerprint(self.config),
            grant=grant,
            target=str(target or "").strip(),
            audit=report,
            changed=changed,
        )

    def render(self, plan: BackupPlan) -> BackupOutcome:
        if not plan.ok or plan.manifest is None:
            return BackupOutcome(False, reason=plan.reason or "备份没列出来")
        lines = ["备份清单（只列名字、不改不删不恢复）："]
        lines.extend("  " + line for line in describe(plan.manifest, limit=self.limit))
        lines.append("")
        lines.append(f"现在的配置指纹：{plan.fingerprint or '（算不出来）'}（只留指纹，不留值）")
        lines.extend(revision_lines(plan.revisions or []))
        if plan.changed:
            lines.append(f"相对上一次记录，配置变了 {len(plan.changed)} 个键：{'、'.join(plan.changed[:6])}")
        else:
            lines.append("相对上一次记录，配置一个键都没变。")
        lines.append("")
        if plan.audit is not None:
            report = plan.audit
            if report.ok and isinstance(report.result, dict):
                lines.append(f"算过了：「{report.target}」这份备份")
                lines.extend("  " + line for line in audit_lines(report.result)[2:])
                lines.append("")
                if report.unsafe:
                    lines.append("我一个字都没改，也没替你恢复：要不要动，你说了算。")
                else:
                    lines.append("这份可以恢复；但真动手还是你自己来——我只负责算账。")
            else:
                lines.append(f"「{report.target}」这份算不了：{report.reason}")
                lines.append("换一份数据备份再算，或者到服务器上跑 `01_部署与运维/restore_check.py`。")
        else:
            lines.append(
                "要恢复之前先算账：发 `/陪伴备份 <名字>`，我会算「这样恢复会不会复活已遗忘的话、"
                "会不会重发已发过的消息」；服务器上也能跑 `01_部署与运维/restore_check.py`。"
            )
        if self.last_error:
            lines.append(f"（这次有一处没读到：{self.last_error}）")
        return BackupOutcome(True, text="\n".join(redact(line) for line in lines))

    async def render_async(self, plan: BackupPlan) -> BackupOutcome:
        return self.render(plan)

    def status_line(self) -> str:
        where = "、".join(str(path) for path in self.backup_dirs)
        try:
            exists = any(path.exists() for path in self.backup_dirs)
        except OSError:
            exists = False
        size_note = "" if exists else "（目录还没建出来）"
        changed = self.changed_since_last()
        if changed:
            delta = f"相对上次变了 {len(changed)} 个键（{'、'.join(changed[:4])}）"
        else:
            delta = "与上次记录相同"
        return (
            f"备份：看 {where}{size_note}；配置{delta}；"
            f"revision 记在 {self.revision_path.name}（只记键名与摘要）"
        )

    # ---- 小工具（给 main 用） ---------------------------------------
    @staticmethod
    def size_text(size: int) -> str:
        return human_size(size)


__all__ = [
    "BackupTurn",
    "BackupPlan",
    "BackupAudit",
    "BackupOutcome",
    "REVISION_FILE",
    "HASH_LIMIT_BYTES",
]
