"""M9-F：文字游戏「三招」的规则层。

纯函数、纯数据：不读写文件、不连网、不碰时钟。所有判定都在这一个文件里，
所以「谁赢」可以被单测钉死，而不是靠模型现场发挥。

规则（双方同时出招，逐回合结算）：

| 出招 | 效果 |
| --- | --- |
| 攻 | 造成 `2 + 蓄力` 点伤害，蓄力清零 |
| 守 | 本回合受到的「攻」伤害先减 `2`（最低 0），并对出「攻」的一方造成 `1` 点反震 |
| 蓄 | 蓄力 +1（最多 2），本回合不造成伤害；挨打也不会丢掉蓄力 |

- 攻 vs 攻：双方各吃对方的伤害（对拼）。
- 攻 vs 守：守方只吃「伤害 - 2」，攻方吃 1 点反震 → **蓄力不够时硬攻不划算**。
- 攻 vs 蓄：蓄方吃满伤害，但蓄力保留 → **蓄力到 2 再攻（4 点）就能破守**。
- 守 vs 守 / 守 vs 蓄 / 蓄 vs 蓄：这一回合谁也不掉血（各自蓄力照算）。

血量到 0 就分胜负；打满回合数就比血量，一样多算平局。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

MOVE_ATTACK = "攻"
MOVE_GUARD = "守"
MOVE_CHARGE = "蓄"
MOVES = (MOVE_ATTACK, MOVE_GUARD, MOVE_CHARGE)

#: 认得的写法（大小写无关；也认英文与数字外的常用别名）
ALIASES = {
    "攻": MOVE_ATTACK,
    "打": MOVE_ATTACK,
    "攻击": MOVE_ATTACK,
    "出招": MOVE_ATTACK,
    "a": MOVE_ATTACK,
    "attack": MOVE_ATTACK,
    "守": MOVE_GUARD,
    "防": MOVE_GUARD,
    "防御": MOVE_GUARD,
    "挡": MOVE_GUARD,
    "g": MOVE_GUARD,
    "guard": MOVE_GUARD,
    "蓄": MOVE_CHARGE,
    "蓄力": MOVE_CHARGE,
    "攒": MOVE_CHARGE,
    "c": MOVE_CHARGE,
    "charge": MOVE_CHARGE,
}

START_HP = 10
MAX_CHARGE = 2
ATTACK_DAMAGE = 2
GUARD_BLOCK = 2
COUNTER_CHIP = 1
MAX_TURNS = 12

STATUS_PLAYING = "playing"
STATUS_WON = "won"
STATUS_LOST = "lost"
STATUS_DRAW = "draw"
STATUS_ABANDONED = "abandoned"
FINISHED = (STATUS_WON, STATUS_LOST, STATUS_DRAW, STATUS_ABANDONED)


class GameError(ValueError):
    """规则层的拒绝：看不懂的动作、不合法的局面。"""


@dataclass(frozen=True)
class TurnResult:
    """一回合结算完的样子（全部是「结算后」的值）。"""

    turn: int
    player_move: str
    her_move: str
    damage_to_player: int
    damage_to_her: int
    player_hp: int
    her_hp: int
    player_charge: int
    her_charge: int
    status: str
    note: str = ""

    @property
    def finished(self) -> bool:
        return self.status in FINISHED

    def to_dict(self) -> dict:
        return {
            "turn": self.turn,
            "player_move": self.player_move,
            "her_move": self.her_move,
            "damage_to_player": self.damage_to_player,
            "damage_to_her": self.damage_to_her,
            "player_hp": self.player_hp,
            "her_hp": self.her_hp,
            "player_charge": self.player_charge,
            "her_charge": self.her_charge,
            "status": self.status,
            "note": self.note,
        }


@dataclass(frozen=True)
class GameState:
    """一局的全部状态。`turn` 是**已结算**的回合数。"""

    game_id: str
    owner_id: str
    session_id: str
    seed: str
    turn: int = 0
    player_hp: int = START_HP
    her_hp: int = START_HP
    player_charge: int = 0
    her_charge: int = 0
    status: str = STATUS_PLAYING
    max_turns: int = MAX_TURNS
    created_at: str = ""
    updated_at: str = ""

    @property
    def finished(self) -> bool:
        return self.status in FINISHED

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "owner_id": self.owner_id,
            "session_id": self.session_id,
            "seed": self.seed,
            "turn": self.turn,
            "player_hp": self.player_hp,
            "her_hp": self.her_hp,
            "player_charge": self.player_charge,
            "her_charge": self.her_charge,
            "status": self.status,
            "max_turns": self.max_turns,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def new_game_id(owner_id: str, session_id: str, seed: str, *, ordinal: int = 0) -> str:
    """一局一个 id。

    带上 `ordinal`（这是这个会话里的第几局）：同一秒里连开两局时间戳一样，
    不带序号就会算出同一个 id、第二局撞主键写不进去。
    """
    raw = f"{owner_id}|{session_id}|{seed}|{int(ordinal)}".encode("utf-8")
    return "g" + hashlib.sha256(raw).hexdigest()[:12]


def parse_move(text: str) -> str:
    """把主人输入的一个词换成合法动作；认不出就抛 `GameError`。

    认三种写法：整个词就是别名（`攻` / `attack`）、词首是汉字招（`攻啊` 也算）、
    带空格时只看第一个词（`攻 出去` 也算）。多词英文短语不猜。
    """
    raw = (text or "").strip().lower()
    if not raw:
        raise GameError("没说这一招出什么：攻、守、蓄，三个里挑一个")
    parts = raw.split()
    head = parts[0] if parts else raw
    if head in ALIASES and len(parts) <= 1:
        return ALIASES[head]
    # 汉字招允许带尾巴：「攻啊」「守一下」
    if head and head[0] in ALIASES and "\u4e00" <= head[0] <= "\u9fff":
        return ALIASES[head[0]]
    raise GameError(f"没看懂「{(text or '').strip()[:12]}」是哪个动作：只有攻、守、蓄")


def _charge_of(move: str, charge: int) -> int:
    if move == MOVE_CHARGE:
        return min(int(charge) + 1, MAX_CHARGE)
    if move == MOVE_ATTACK:
        return 0
    return int(charge)


def _attack_damage(charge: int) -> int:
    return ATTACK_DAMAGE + min(int(charge), MAX_CHARGE)


def her_move_for(state: GameState, *, seed_extra: str = "") -> str:
    """她的出招：由 `(seed, turn)` 定死，同一局同一回合永远一样。

    策略很朴素，但可解释：血少优先守，蓄着力优先放，没蓄力有时蓄一手。
    """
    rng = random.Random(f"{state.seed}:{int(state.turn)}:{seed_extra}")
    roll = rng.random()
    if state.her_hp <= 2 and roll < 0.60:
        return MOVE_GUARD
    if state.her_charge >= 1 and roll < 0.55:
        return MOVE_ATTACK
    if state.her_charge < MAX_CHARGE and roll < 0.35:
        return MOVE_CHARGE
    return MOVE_ATTACK


def _note(player_move: str, her_move: str, to_player: int, to_her: int) -> str:
    if player_move == her_move:
        if player_move == MOVE_ATTACK:
            return "两个人都攻了：对拼"
        if player_move == MOVE_GUARD:
            return "两个人都守着：谁也进不来"
        return "两个人都蓄了一手"
    if player_move == MOVE_ATTACK and her_move == MOVE_GUARD:
        return "她守住了，你被反震了一下" if to_player else "她守住了"
    if player_move == MOVE_GUARD and her_move == MOVE_ATTACK:
        return "你守住了，她被反震了一下" if to_her else "你守住了"
    if player_move == MOVE_ATTACK and her_move == MOVE_CHARGE:
        return "她正在蓄力，被你打断了节奏（蓄力还在）"
    if player_move == MOVE_CHARGE and her_move == MOVE_ATTACK:
        return "你正在蓄力，挨了她一下（蓄力还在）"
    if player_move == MOVE_GUARD and her_move == MOVE_CHARGE:
        return "她蓄了一手，你没动"
    if player_move == MOVE_CHARGE and her_move == MOVE_GUARD:
        return "你蓄了一手，她没动"
    return ""


def resolve(state: GameState, player_move: str) -> TurnResult:
    """结算一回合。`state` 必须是进行中的局面，否则抛 `GameError`。"""
    if state.finished:
        raise GameError("这一局已经结束了")
    if player_move not in MOVES:
        raise GameError(f"{player_move} 不是合法动作：只有攻、守、蓄")

    her_move = her_move_for(state)
    to_her = 0
    to_player = 0

    if player_move == MOVE_ATTACK:
        to_her = _attack_damage(state.player_charge)
    if her_move == MOVE_ATTACK:
        to_player = _attack_damage(state.her_charge)

    if her_move == MOVE_GUARD and player_move == MOVE_ATTACK:
        to_her = max(0, to_her - GUARD_BLOCK)
        to_player += COUNTER_CHIP
    if player_move == MOVE_GUARD and her_move == MOVE_ATTACK:
        to_player = max(0, to_player - GUARD_BLOCK)
        to_her += COUNTER_CHIP

    player_hp = max(0, state.player_hp - to_player)
    her_hp = max(0, state.her_hp - to_her)

    turn = state.turn + 1
    if her_hp <= 0 and player_hp <= 0:
        status = STATUS_DRAW
    elif her_hp <= 0:
        status = STATUS_WON
    elif player_hp <= 0:
        status = STATUS_LOST
    elif turn >= max(1, int(state.max_turns)):
        if player_hp > her_hp:
            status = STATUS_WON
        elif her_hp > player_hp:
            status = STATUS_LOST
        else:
            status = STATUS_DRAW
    else:
        status = STATUS_PLAYING

    return TurnResult(
        turn=turn,
        player_move=player_move,
        her_move=her_move,
        damage_to_player=to_player,
        damage_to_her=to_her,
        player_hp=player_hp,
        her_hp=her_hp,
        player_charge=_charge_of(player_move, state.player_charge),
        her_charge=_charge_of(her_move, state.her_charge),
        status=status,
        note=_note(player_move, her_move, to_player, to_her),
    )


def apply(state: GameState, result: TurnResult, *, now_iso: str = "") -> GameState:
    """把结算结果写回状态（不改状态机细节，只换字段）。"""
    return GameState(
        game_id=state.game_id,
        owner_id=state.owner_id,
        session_id=state.session_id,
        seed=state.seed,
        turn=result.turn,
        player_hp=result.player_hp,
        her_hp=result.her_hp,
        player_charge=result.player_charge,
        her_charge=result.her_charge,
        status=result.status,
        max_turns=state.max_turns,
        created_at=state.created_at,
        updated_at=now_iso or state.updated_at,
    )


def abandon(state: GameState, *, now_iso: str = "") -> GameState:
    if state.finished:
        raise GameError("这一局已经结束了")
    return GameState(
        game_id=state.game_id,
        owner_id=state.owner_id,
        session_id=state.session_id,
        seed=state.seed,
        turn=state.turn,
        player_hp=state.player_hp,
        her_hp=state.her_hp,
        player_charge=state.player_charge,
        her_charge=state.her_charge,
        status=STATUS_ABANDONED,
        max_turns=state.max_turns,
        created_at=state.created_at,
        updated_at=now_iso or state.updated_at,
    )


def describe_state(state: GameState, *, who: str = "她") -> str:
    """一行能看见的局势。**每一回合都要报一次**，不许「你猜」。"""
    head = f"第 {state.turn + 1} 回合 · 你 {state.player_hp}/{START_HP}"
    if state.player_charge:
        head += f"（蓄 {state.player_charge}）"
    head += f" · {who} {state.her_hp}/{START_HP}"
    if state.her_charge:
        head += f"（蓄 {state.her_charge}）"
    return head


def describe_result(result: TurnResult, *, who: str = "她") -> str:
    """一句人话：你出了什么、她出了什么、各自掉了多少血。"""
    bits = [f"你出「{result.player_move}」，{who}出「{result.her_move}」"]
    if result.damage_to_her:
        bits.append(f"{who}掉了 {result.damage_to_her} 点")
    if result.damage_to_player:
        bits.append(f"你掉了 {result.damage_to_player} 点")
    if not result.damage_to_her and not result.damage_to_player:
        bits.append("这一回合谁也没掉血")
    line = "，".join(bits)
    if result.note:
        line += f"（{result.note}）"
    return line


def verdict_line(state: GameState, *, who: str = "她") -> str:
    """结局一句话。"""
    if state.status == STATUS_WON:
        return f"你赢了：{who}的血先见底（你 {state.player_hp}，她 {state.her_hp}）。"
    if state.status == STATUS_LOST:
        return f"{who}赢了：你的血先见底（你 {state.player_hp}，她 {state.her_hp}）。"
    if state.status == STATUS_DRAW:
        return f"平局：你 {state.player_hp}，她 {state.her_hp}。"
    if state.status == STATUS_ABANDONED:
        return f"这一局认输了（打到第 {state.turn} 回合）。"
    return ""


__all__ = [
    "ALIASES",
    "ATTACK_DAMAGE",
    "COUNTER_CHIP",
    "FINISHED",
    "GUARD_BLOCK",
    "GameError",
    "GameState",
    "MAX_CHARGE",
    "MAX_TURNS",
    "MOVES",
    "MOVE_ATTACK",
    "MOVE_CHARGE",
    "MOVE_GUARD",
    "START_HP",
    "STATUS_ABANDONED",
    "STATUS_DRAW",
    "STATUS_LOST",
    "STATUS_PLAYING",
    "STATUS_WON",
    "TurnResult",
    "abandon",
    "apply",
    "describe_result",
    "describe_state",
    "her_move_for",
    "new_game_id",
    "parse_move",
    "resolve",
    "verdict_line",
]
