"""陪伴插件自己的小账本（不碰记忆库）。

只存三类东西：
- `owner_session`：主人最近一次私聊的会话标识（`平台:FriendMessage:QQ号`），
  主动消息要有地方发，就得记住这个；
- `pinged_at`：上线自检那句话发过没有（一辈子只发一次，重启不重复）；
- `last_tick_at`：上一次主动检查的时间，纯排障用。

它是 JSON 文件，用「写临时文件再 `os.replace`」的方式落盘：写到一半断电也不会
留下半个坏账本。坏了就当空的读，绝不让插件因为账本损坏而起不来。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class JsonState:
    """极小的键值账本，原子落盘。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = self._load()

    # ---- 读写 -------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any, *, save: bool = True) -> None:
        self._data[key] = value
        if save:
            self.save()

    def update(self, values: dict[str, Any], *, save: bool = True) -> None:
        self._data.update(values)
        if save:
            self.save()

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    # ---- 落盘 -------------------------------------------------------------
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(self._data, fp, ensure_ascii=False, indent=2, sort_keys=True)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}


def default_state_path(base: str | Path | None = None) -> Path:
    """账本默认放 `AstrBot/data/plugin_data/<插件名>/state.json` 旁边。"""
    if base:
        return Path(base) / "state.json"
    return Path("data") / "plugin_data" / "astrbot_plugin_feiyu_companion" / "state.json"
