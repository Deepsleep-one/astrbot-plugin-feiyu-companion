"""直播只读端口（M9-E）：一次拉一份公开状态，不登录、不进房间、不解析弹幕。

设计上的三条硬线：

1. **只要公开只读事实**：房间号、标题、分区、开播状态、在线人数、关注数。
   弹幕、礼物、观众昵称一概不要——那些既吵又容易被别人的话牵着走。
2. **不拿主人账号**：请求不带 Cookie、不带 Authorization，只用一个说明自己是只读的
   User-Agent。要登录才能看的东西，这里直接说不看。
3. **一次一份 + 有效期**：快照带 `ttl_seconds`，过了就作废，不拿旧人数冒充现在。

这个模块不联网。真正发请求的是 `adapters/live_bili.py`，它复用的还是 `web_http.py`
那条已经做过逐跳私网校验的读页通道。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

LIVE_PORT_NAME = "LivePortV1"
LIVE_VERSION = 1

DEFAULT_TTL_SECONDS = 60
MAX_TTL_SECONDS = 300
MAX_TITLE = 80
MAX_AREA = 24
MAX_ROOM_ID = 999_999_999_999

#: 直播间地址的几种写法：纯数字、live.bilibili.com/<id>、带 ?query 或 /h5 之类
_ROOM_URL_RE = re.compile(r"live\.bilibili\.com/(?:h5/)?(\d{1,12})", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^\d{1,12}$")

_NOTE_LIMIT = 4


class LiveError(ValueError):
    """请求本身不成立（房间号不对、场景不对）。"""


class LiveUnavailable(RuntimeError):
    """这一次拿不到（没开、没源、网络不行、对方拒绝）。"""


def parse_room(text: str) -> int:
    """从「21452505」或「https://live.bilibili.com/21452505?x=1」里取出房间号。"""
    raw = str(text or "").strip()
    if not raw:
        raise LiveError("要看哪个直播间？给我房间号，或者直播间地址")
    match = _ROOM_URL_RE.search(raw)
    if match:
        return _checked(int(match.group(1)))
    if _DIGITS_RE.match(raw):
        return _checked(int(raw))
    raise LiveError(f"没看懂这个房间号：{raw[:40]}（只认数字，或者 live.bilibili.com 的地址）")


def _checked(room_id: int) -> int:
    if room_id <= 0 or room_id > MAX_ROOM_ID:
        raise LiveError(f"房间号超出范围：{room_id}")
    return room_id


def _int(value: Any, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _text(value: Any, *, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    # 标题里偶尔会夹带 HTML 实体或标签，粗抹一遍：端口层不解析富文本
    text = re.sub(r"<[^>]{1,200}>", "", text)
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    if len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return text


def human_count(value: int) -> str:
    """139420 → 13.9 万；999 → 999。只做展示，不改数值。"""
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f} 亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f} 万"
    return str(value)


@dataclass(frozen=True)
class LiveQuery:
    room_id: int
    scene: str = "private"
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    def __post_init__(self) -> None:
        if self.scene != "private":
            raise LiveError("直播间的状态只在私聊里看：群里不替主人开别人的直播")
        object.__setattr__(self, "room_id", _checked(int(self.room_id)))
        ttl = DEFAULT_TTL_SECONDS if self.ttl_seconds is None else int(self.ttl_seconds)
        if ttl < 1 or ttl > MAX_TTL_SECONDS:
            raise LiveError(f"有效期要在 1–{MAX_TTL_SECONDS} 秒之间")
        object.__setattr__(self, "ttl_seconds", ttl)

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": LIVE_PORT_NAME,
            "version": LIVE_VERSION,
            "room_id": self.room_id,
            "scene": self.scene,
            "ttl_seconds": self.ttl_seconds,
        }


@dataclass(frozen=True)
class LiveSnapshot:
    """一份公开的直播间状态。字段是白名单，别的都不进来。"""

    ok: bool
    room_id: int
    title: str = ""
    anchor: str = ""
    parent_area: str = ""
    area: str = ""
    live: bool = False
    online: int = 0
    followers: int = 0
    captured_at: str = ""
    expires_at: str = ""
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return bool(self.live)

    def is_expired(self, now) -> bool:
        if not self.expires_at:
            return False
        from datetime import datetime

        try:
            deadline = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return True
        return now >= deadline

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": LIVE_PORT_NAME,
            "version": LIVE_VERSION,
            "ok": self.ok,
            "room_id": self.room_id,
            "title": self.title,
            "anchor": self.anchor,
            "parent_area": self.parent_area,
            "area": self.area,
            "live": self.live,
            "online": self.online,
            "followers": self.followers,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
            "ttl_seconds": self.ttl_seconds,
            "notes": list(self.notes[: _NOTE_LIMIT]),
        }


def failed(reason: str, *, room_id: int = 0) -> LiveSnapshot:
    return LiveSnapshot(ok=False, room_id=room_id, notes=[str(reason or "没看成")[:200]])


def normalize_snapshot(raw: Any, query: LiveQuery, *, now=None) -> LiveSnapshot:
    """把对方回的那坨 JSON 压成白名单快照。看不懂就抛，不猜。"""
    from datetime import datetime, timedelta, timezone

    if not isinstance(raw, dict):
        raise LiveUnavailable("直播接口回的东西不是对象")
    code = raw.get("code")
    if code is not None and _int(code, -1) != 0:
        message = _text(raw.get("message") or raw.get("msg") or "", limit=60)
        raise LiveUnavailable(f"直播接口拒绝了这次查询（code={code}{'：' + message if message else ''}）")

    data = raw.get("data")
    if not isinstance(data, dict):
        raise LiveUnavailable("直播接口没给 data")

    room = _int(data.get("room_id"), query.room_id) or query.room_id
    live_status = _int(data.get("live_status"), 0)

    anchor = ""
    anchor_info = data.get("anchor_info")
    if isinstance(anchor_info, dict):
        base = anchor_info.get("base_info")
        if isinstance(base, dict):
            anchor = _text(base.get("uname") or "", limit=24)
    if not anchor:
        anchor = _text(data.get("uname") or "", limit=24)

    moment = now or datetime.now(timezone(timedelta(hours=8)))
    captured = moment.isoformat()
    expires = (moment + timedelta(seconds=query.ttl_seconds)).isoformat()

    notes: list[str] = []
    if live_status not in (0, 1, 2):
        notes.append(f"不认识的开播状态：{live_status}")
    if live_status == 2:
        notes.append("这个直播间在轮播（不是主播本人在播）")

    return LiveSnapshot(
        ok=True,
        room_id=int(room),
        title=_text(data.get("title") or "", limit=MAX_TITLE) or "（没有标题）",
        anchor=anchor,
        parent_area=_text(data.get("parent_area_name") or "", limit=MAX_AREA),
        area=_text(data.get("area_name") or "", limit=MAX_AREA),
        live=live_status == 1,
        online=_int(data.get("online"), 0),
        followers=_int(data.get("attention"), 0),
        captured_at=captured,
        expires_at=expires,
        ttl_seconds=query.ttl_seconds,
        notes=notes[: _NOTE_LIMIT],
    )


def describe(snapshot: LiveSnapshot) -> str:
    """把快照写成人话。只说读到的事实，不猜内容、不转述弹幕。"""
    if not snapshot.ok:
        reason = snapshot.notes[0] if snapshot.notes else "没看成"
        return f"没看成：{reason}"

    lines = [f"直播间 {snapshot.room_id}："]
    where = " · ".join(part for part in (snapshot.parent_area, snapshot.area) if part)
    if where:
        lines.append(f"分区：{where}")
    if snapshot.live:
        lines.append(f"正在播《{snapshot.title}》")
    else:
        lines.append(f"现在没在播（{snapshot.title}）")
    if snapshot.anchor:
        lines.append(f"主播：{snapshot.anchor}")
    numbers = []
    if snapshot.online:
        numbers.append(f"在线 {human_count(snapshot.online)}")
    if snapshot.followers:
        numbers.append(f"关注 {human_count(snapshot.followers)}")
    if numbers:
        lines.append("，".join(numbers))
    for note in snapshot.notes[:_NOTE_LIMIT]:
        lines.append(f"（{note}）")
    lines.append("（这是公开的只读状态：没进房间、没发弹幕、没看内容。）")
    return "\n".join(lines)


__all__ = [
    "LIVE_PORT_NAME",
    "LIVE_VERSION",
    "DEFAULT_TTL_SECONDS",
    "MAX_TTL_SECONDS",
    "LiveError",
    "LiveUnavailable",
    "LiveQuery",
    "LiveSnapshot",
    "failed",
    "parse_room",
    "normalize_snapshot",
    "describe",
    "human_count",
]
