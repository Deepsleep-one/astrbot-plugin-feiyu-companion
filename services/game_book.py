"""M9-F：对局的存档（一个独立的 SQLite 文件）。

为什么单独一个库：记忆库归记忆插件、B站库归 B站插件，**对局存档哪儿都不该塞**。
这里只存两种东西——「一局的状态」和「每一回合两人出了什么」，
没有网络、没有账号、没有主人事实，删了这个文件最多是「这局没了」。

写入只有三条路径：`create`（开局）、`save`（每回合 / 认输）、`record`（记一回合）。
其余全是只读查询。表结构加列不改语义（下一次迁移用 `_ensure_columns`）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from ..ports.game import (
        FINISHED,
        STATUS_PLAYING,
        GameError,
        GameState,
        TurnResult,
    )
except ImportError:  # 顶层兜底
    from astrbot_plugin_feiyu_companion.ports.game import (
        FINISHED,
        STATUS_PLAYING,
        GameError,
        GameState,
        TurnResult,
    )

TZ = timezone(timedelta(hours=8))
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id       TEXT PRIMARY KEY,
    owner_id      TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    seed          TEXT NOT NULL,
    turn          INTEGER NOT NULL DEFAULT 0,
    player_hp     INTEGER NOT NULL,
    her_hp        INTEGER NOT NULL,
    player_charge INTEGER NOT NULL DEFAULT 0,
    her_charge    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL,
    max_turns     INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_games_session ON games(owner_id, session_id, status);
CREATE INDEX IF NOT EXISTS idx_games_owner ON games(owner_id, created_at);

CREATE TABLE IF NOT EXISTS moves (
    game_id          TEXT NOT NULL,
    turn             INTEGER NOT NULL,
    player_move      TEXT NOT NULL,
    her_move         TEXT NOT NULL,
    damage_to_player INTEGER NOT NULL DEFAULT 0,
    damage_to_her    INTEGER NOT NULL DEFAULT 0,
    note             TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    PRIMARY KEY (game_id, turn)
);
"""


def _now(clock=None) -> datetime:
    if clock is not None and hasattr(clock, "now"):
        return clock.now()
    return datetime.now(TZ)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


class GameBook:
    """对局存档：开局、读当前局、存一回合、翻历史。"""

    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        self.clock = clock
        self.last_error = ""
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=5)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._db.commit()

    # ---- 基础 -------------------------------------------------------
    def close(self) -> None:
        try:
            self._db.close()
        except Exception:  # noqa: BLE001
            pass

    @property
    def db(self) -> sqlite3.Connection:
        return self._db

    def _row_to_state(self, row: sqlite3.Row) -> GameState:
        return GameState(
            game_id=row["game_id"],
            owner_id=row["owner_id"],
            session_id=row["session_id"],
            seed=row["seed"],
            turn=int(row["turn"]),
            player_hp=int(row["player_hp"]),
            her_hp=int(row["her_hp"]),
            player_charge=int(row["player_charge"]),
            her_charge=int(row["her_charge"]),
            status=row["status"],
            max_turns=int(row["max_turns"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ---- 写 ---------------------------------------------------------
    def create(self, state: GameState) -> GameState:
        """开一局。同一会话已有进行中的局就不许开新的（先认输或打完）。"""
        with self._db:
            existing = self.active(state.owner_id, state.session_id)
            if existing is not None:
                raise GameError("这一局还没结束：先出招，或者 /陪伴认输")
            moment = _iso(_now(self.clock))
            self._db.execute(
                "INSERT INTO games (game_id, owner_id, session_id, seed, turn, player_hp,"
                " her_hp, player_charge, her_charge, status, max_turns, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    state.game_id,
                    state.owner_id,
                    state.session_id,
                    state.seed,
                    int(state.turn),
                    int(state.player_hp),
                    int(state.her_hp),
                    int(state.player_charge),
                    int(state.her_charge),
                    state.status,
                    int(state.max_turns),
                    state.created_at or moment,
                    moment,
                ),
            )
        return self.get(state.game_id) or state

    def save(self, state: GameState) -> GameState:
        with self._db:
            cur = self._db.execute(
                "UPDATE games SET turn=?, player_hp=?, her_hp=?, player_charge=?,"
                " her_charge=?, status=?, updated_at=? WHERE game_id=?",
                (
                    int(state.turn),
                    int(state.player_hp),
                    int(state.her_hp),
                    int(state.player_charge),
                    int(state.her_charge),
                    state.status,
                    _iso(_now(self.clock)),
                    state.game_id,
                ),
            )
            if cur.rowcount == 0:
                raise GameError("这一局不在存档里")
        return self.get(state.game_id) or state

    def record(self, result: TurnResult, *, game_id: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO moves (game_id, turn, player_move, her_move,"
                " damage_to_player, damage_to_her, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    game_id,
                    int(result.turn),
                    result.player_move,
                    result.her_move,
                    int(result.damage_to_player),
                    int(result.damage_to_her),
                    result.note[:200],
                    _iso(_now(self.clock)),
                ),
            )

    # ---- 读 ---------------------------------------------------------
    def get(self, game_id: str) -> GameState | None:
        row = self._db.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
        return self._row_to_state(row) if row else None

    def active(self, owner_id: str, session_id: str) -> GameState | None:
        """这个会话里还没打完的那一局（最多一局）。"""
        row = self._db.execute(
            "SELECT * FROM games WHERE owner_id=? AND session_id=? AND status=?"
            " ORDER BY created_at DESC LIMIT 1",
            (owner_id, session_id, STATUS_PLAYING),
        ).fetchone()
        return self._row_to_state(row) if row else None

    def started_today(self, owner_id: str, *, now: datetime | None = None) -> int:
        moment = now or _now(self.clock)
        day = moment.strftime("%Y-%m-%d")
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM games WHERE owner_id=? AND substr(created_at,1,10)=?",
            (owner_id, day),
        ).fetchone()
        return int(row["n"] if row else 0)

    def count_for(self, owner_id: str, session_id: str) -> int:
        """这个会话里一共开过几局（含没打完的）。给 game_id 加序号用。"""
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM games WHERE owner_id=? AND session_id=?",
            (owner_id, session_id),
        ).fetchone()
        return int(row["n"] if row else 0)

    def history(self, game_id: str, *, limit: int = 20) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM moves WHERE game_id=? ORDER BY turn ASC LIMIT ?",
            (game_id, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def finished_count(self, owner_id: str) -> int:
        marks = ",".join("?" for _ in FINISHED)
        row = self._db.execute(
            f"SELECT COUNT(*) AS n FROM games WHERE owner_id=? AND status IN ({marks})",
            (owner_id, *FINISHED),
        ).fetchone()
        return int(row["n"] if row else 0)


__all__ = ["GameBook", "SCHEMA_VERSION"]
