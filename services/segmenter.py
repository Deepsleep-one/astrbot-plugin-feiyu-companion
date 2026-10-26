"""分段回复：把一句话拆成几条短消息，中间按打字速度停一下。

照搬旧插件（``astrbot_plugin_proactive_chat``）的经验，但补了三个它没有的保险：

1. **清理规则永远不会把消息清空**——如果清理后一句不剩，退回原文，宁可发整句也不发空；
2. 正则写错不会炸，退回默认分段；
3. 间隔只在段与段之间，首段不等、末段之后不等。

注意一个已知的坑：``content_cleanup_rule`` 如果写成 ``.*?[。…]``，会把每一段整段删成空串，
表现为「库里写了、平台没出站」。这里默认留空，并且在规则把所有段清空时自动退回原文。
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

DEFAULT_SPLIT_REGEX = r".*?[。？！~…\n]+|.+$"
DEFAULT_SPLIT_WORDS = ("。", "？", "！", "~", "…")


@dataclass(frozen=True)
class SegmentPlan:
    """一次分段的结果：发几段、每段之间停多久。"""

    segments: tuple[str, ...]
    intervals: tuple[float, ...] = ()
    note: str = ""

    @property
    def count(self) -> int:
        return len(self.segments)

    @property
    def total_wait(self) -> float:
        return sum(self.intervals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "total_wait": round(self.total_wait, 3),
            "segments": list(self.segments),
            "intervals": [round(item, 3) for item in self.intervals],
            "note": self.note,
        }


@dataclass
class Segmenter:
    """把文本切成若干段，并算出段间间隔。"""

    enabled: bool = True
    threshold: int = 80
    mode: str = "regex"
    regex: str = DEFAULT_SPLIT_REGEX
    split_words: Sequence[str] = DEFAULT_SPLIT_WORDS
    cleanup_enable: bool = False
    cleanup_rule: str = ""
    interval_mode: str = "log"
    interval: str = "1.5, 3.5"
    log_base: float = 1.8
    rng: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.rng is None:
            self.rng = random.Random()

    # ------------------------------------------------------------------
    def _cleanup_pattern(self) -> re.Pattern[str] | None:
        if not self.cleanup_enable:
            return None
        rule = str(self.cleanup_rule or "").strip()
        if not rule:
            return None
        try:
            return re.compile(rule)
        except re.error:
            return None

    def split(self, text: str) -> list[str]:
        """切段。返回至少一段（原文）。"""
        body = str(text or "").strip()
        if not body:
            return []
        if not self.enabled:
            return [body]

        threshold = int(self.threshold or 0)
        if threshold > 0 and len(body) > threshold:
            return [body]

        pattern = self._cleanup_pattern()
        if self.mode == "words":
            words = [str(word) for word in (self.split_words or DEFAULT_SPLIT_WORDS) if str(word)]
            if not words:
                return [body]
            escaped = sorted((re.escape(word) for word in words), key=len, reverse=True)
            join = "|".join(escaped)
            try:
                matches = re.findall(f"(.*?({join})|.+$)", body, re.DOTALL)
            except re.error:
                return [body]
            pieces = [item[0] if isinstance(item, tuple) else item for item in matches]
        else:
            try:
                pieces = re.findall(self.regex or DEFAULT_SPLIT_REGEX, body, re.DOTALL | re.MULTILINE)
            except re.error:
                pieces = re.findall(DEFAULT_SPLIT_REGEX, body, re.DOTALL | re.MULTILINE)

        result: list[str] = []
        for piece in pieces:
            if not isinstance(piece, str):
                continue
            cleaned = pattern.sub("", piece) if pattern else piece
            if cleaned.strip():
                result.append(cleaned.strip())
        return result or [body]

    # ------------------------------------------------------------------
    def interval_for(self, text: str) -> float:
        """按字数算一个手感上像人的停顿（秒）。"""
        body = str(text or "")
        if str(self.interval_mode or "") == "log":
            try:
                base = float(self.log_base or 1.8)
            except (TypeError, ValueError):
                base = 1.8
            if base <= 1.0:
                base = 1.8
            if all(ord(char) < 128 for char in body):
                count = len([part for part in body.split() if part])
            else:
                count = len([char for char in body if char.isalnum() or char.isalpha()])
            spot = math.log(count + 1, base)
            return round(self.rng.uniform(spot, spot + 0.5), 3)

        bounds = self._interval_bounds()
        return round(self.rng.uniform(bounds[0], bounds[1]), 3)

    def _interval_bounds(self) -> tuple[float, float]:
        raw = str(self.interval or "").replace(" ", "")
        try:
            parts = [float(item) for item in raw.split(",") if item]
        except (TypeError, ValueError):
            parts = []
        if len(parts) != 2:
            return (1.5, 3.5)
        low, high = sorted(parts)
        return (max(0.0, low), max(0.0, high))

    # ------------------------------------------------------------------
    def plan(self, text: str) -> SegmentPlan:
        """一次性拿到「发几段 + 每段之间停多久」。"""
        segments = self.split(text)
        if len(segments) <= 1:
            return SegmentPlan(tuple(segments) or (str(text or "").strip(),))
        intervals = tuple(self.interval_for(segment) for segment in segments[:-1])
        return SegmentPlan(tuple(segments), intervals)

    def join(self, segments: Iterable[str]) -> str:
        return "".join(str(item) for item in segments)


def segmenter_from_config(config: dict[str, Any]) -> Segmenter:
    """从插件配置造一个分段器（键名与 _conf_schema.json 对齐）。"""

    def _bool(key: str, default: bool) -> bool:
        value = config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def _int(key: str, default: int) -> int:
        try:
            return int(config.get(key, default) or 0)
        except (TypeError, ValueError):
            return default

    def _float(key: str, default: float) -> float:
        try:
            return float(config.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    words = config.get("segment_split_words") or DEFAULT_SPLIT_WORDS
    if isinstance(words, str):
        if "," in words:
            words = tuple(item.strip() for item in words.split(",") if item.strip())
        else:
            # 老插件这个字段是一串标点（"。？！~…"），逐字当断点才符合直觉；
            # 只在写了逗号时才按逗号分组（允许 "……" 这种多字符断点）。
            words = tuple(char for char in words.strip() if not char.isspace())
    else:
        words = tuple(str(item) for item in words)
    if not words:
        words = DEFAULT_SPLIT_WORDS

    return Segmenter(
        enabled=_bool("segment_enable", False),
        threshold=_int("segment_threshold", 80),
        mode=str(config.get("segment_mode") or "regex"),
        regex=str(config.get("segment_regex") or DEFAULT_SPLIT_REGEX),
        split_words=words or DEFAULT_SPLIT_WORDS,
        cleanup_enable=_bool("segment_cleanup_enable", False),
        cleanup_rule=str(config.get("segment_cleanup_rule") or ""),
        interval_mode=str(config.get("segment_interval_mode") or "log"),
        interval=str(config.get("segment_interval") or "1.5, 3.5"),
        log_base=_float("segment_log_base", 1.8),
    )
