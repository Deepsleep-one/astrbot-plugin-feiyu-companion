"""从 AstrBot 读历史：LLM 对话历史（私聊主力）与平台聊天流水（群聊备选）。

两个来源的差别，写在这里免得以后又猜：

- ``conversation_history``：AstrBot 的 LLM 对话历史（``conversation_manager``），
  私聊真实在用，里面是 ``[{"role": "user"/"assistant", "content": ...}]``。
- ``platform_message_history``：AstrBot 的「平台聊天流水」（``message_history_manager``），
  默认只对**群聊**开启记录（`group_message_history_enable`），私聊通常是空的。
  所以 hybrid 模式下私聊会自然回落到对话历史，不会假装有流水。

任何异常都退化成空历史 + note，绝不抛给主动循环。
"""

from __future__ import annotations

import json
from typing import Any

from ..ports.history import (
    ROLE_ASSISTANT,
    ROLE_OWNER,
    SOURCE_CONVERSATION,
    SOURCE_HYBRID,
    SOURCE_MODES,
    SOURCE_PLATFORM,
    HistoryLine,
    HistorySlice,
)

DEFAULT_BOT_IDENTIFIERS = ("bot", "astrbot", "assistant")
MAX_TEXT = 300


def split_session(session: str) -> tuple[str, str, str] | None:
    """把 UMO 拆成 (platform_id, message_type, session_id)。"""
    if not isinstance(session, str):
        return None
    parts = session.split(":", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        return None
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


def _clean(text: Any) -> str:
    body = " ".join(str(text or "").split())
    if len(body) > MAX_TEXT:
        body = body[: MAX_TEXT - 1] + "…"
    return body


def _parse_bot_identifiers(value: Any) -> set[str]:
    if isinstance(value, str):
        items = [item.strip().lower() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item).strip().lower() for item in value]
    else:
        items = []
    items = [item for item in items if item]
    return set(items) or set(DEFAULT_BOT_IDENTIFIERS)


def extract_text(content: Any) -> str:
    """宽松取出消息正文（字符串 / 分段列表 / 各种字典）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return _clean(content)
    if isinstance(content, list):
        parts = content
    elif isinstance(content, dict):
        parts = None
        for key in ("message", "chain", "content", "messages"):
            value = content.get(key)
            if isinstance(value, list):
                parts = value
                break
        if parts is None:
            for key in ("text", "message", "content", "plain"):
                value = content.get(key)
                if isinstance(value, str):
                    return _clean(value)
            return ""
    else:
        return _clean(content)

    texts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "").lower()
        if kind in ("plain", "text"):
            texts.append(str(part.get("text") or ""))
        elif kind in ("image", "record", "video", "file"):
            texts.append(f"[{kind}]")
    return _clean("".join(texts))


class AstrBotHistory:
    """AstrBot 的历史只读适配器。"""

    def __init__(
        self,
        context: Any,
        *,
        source: str = SOURCE_HYBRID,
        include_bot_messages: bool = True,
        bot_identifiers: Any = None,
        logger: Any = None,
    ) -> None:
        self.context = context
        self.source = source if source in SOURCE_MODES else SOURCE_HYBRID
        self.include_bot_messages = bool(include_bot_messages)
        self.bot_identifiers = _parse_bot_identifiers(bot_identifiers)
        self.logger = logger

    # ------------------------------------------------------------------
    # 对话历史
    # ------------------------------------------------------------------
    async def _conversation_lines(self, session: str, limit: int) -> list[HistoryLine]:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return []
        conversation_id = await manager.get_curr_conversation_id(session)
        if not conversation_id:
            return []
        conversation = await manager.get_conversation(session, conversation_id)
        raw = getattr(conversation, "history", "") or ""
        if isinstance(raw, str):
            try:
                items = json.loads(raw or "[]")
            except (TypeError, ValueError):
                return []
        else:
            items = raw
        if not isinstance(items, list):
            return []

        lines: list[HistoryLine] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").lower()
            text = extract_text(item.get("content"))
            if not text:
                continue
            if role in ("user", "human"):
                mapped = ROLE_OWNER
            elif role in ("assistant", "bot", "ai"):
                mapped = ROLE_ASSISTANT
            else:
                continue
            lines.append(HistoryLine(role=mapped, text=text))
        return lines[-limit:] if limit > 0 else lines

    # ------------------------------------------------------------------
    # 平台流水
    # ------------------------------------------------------------------
    def _is_bot_record(self, record: Any) -> bool:
        def field(name: str) -> str:
            if isinstance(record, dict):
                return str(record.get(name) or "")
            return str(getattr(record, name, "") or "")

        sender_id = field("sender_id").lower()
        sender_name = field("sender_name").lower()
        content = record.get("content") if isinstance(record, dict) else getattr(
            record, "content", None
        )
        content_type = ""
        if isinstance(content, dict):
            content_type = str(content.get("type") or "").lower()
        return (
            sender_id in self.bot_identifiers
            or sender_name in self.bot_identifiers
            or content_type in self.bot_identifiers
        )

    async def _platform_lines(self, session: str, limit: int) -> list[HistoryLine]:
        manager = getattr(self.context, "message_history_manager", None)
        if manager is None:
            return []
        parsed = split_session(session)
        if parsed is None:
            return []
        platform_id, _, session_id = parsed
        for user_id in (session, session_id):
            try:
                records = await manager.get(
                    platform_id=platform_id,
                    user_id=user_id,
                    page=1,
                    page_size=max(1, min(int(limit), 200)),
                )
            except Exception:
                continue
            if not records:
                continue
            lines: list[HistoryLine] = []
            for record in records:
                is_bot = self._is_bot_record(record)
                if is_bot and not self.include_bot_messages:
                    continue
                content = (
                    record.get("content")
                    if isinstance(record, dict)
                    else getattr(record, "content", None)
                )
                text = extract_text(content)
                if not text:
                    continue
                lines.append(
                    HistoryLine(
                        role=ROLE_ASSISTANT if is_bot else ROLE_OWNER,
                        text=text,
                    )
                )
            if lines:
                return lines
        return []

    # ------------------------------------------------------------------
    async def recent(self, session: str, *, limit: int = 30) -> HistorySlice:
        limit = max(0, int(limit))
        if limit == 0 or not session:
            return HistorySlice(note="没开历史注入")

        if self.source == SOURCE_PLATFORM:
            lines = await self._platform_lines(session, limit)
            if lines:
                return HistorySlice(tuple(lines), SOURCE_PLATFORM)
            return HistorySlice(note="没读到平台流水")

        conversation = await self._conversation_lines(session, limit)
        if self.source == SOURCE_CONVERSATION:
            if conversation:
                return HistorySlice(tuple(conversation), SOURCE_CONVERSATION)
            return HistorySlice(note="没读到对话历史")

        # hybrid：有流水用流水，没有就用对话历史
        platform = await self._platform_lines(session, limit)
        if platform:
            return HistorySlice(tuple(platform), SOURCE_PLATFORM)
        if conversation:
            return HistorySlice(tuple(conversation), SOURCE_CONVERSATION)
        return HistorySlice(note="没读到对话历史，也没有平台流水")
