"""主动消息的措辞：让她自己说，而不是套模板。

借鉴 `astrbot_plugin_proactive_chat` 的做法（只借思路，不搬代码）：

1. **人格与规则放 system_prompt，本轮变量放 user prompt** —— system 是稳定前缀，
   时间/未回复次数/当下在做什么这些每轮都变的东西不污染前缀。
2. **未回复次数进提示词**：连着几条没回就轻一点、收一点，别越说越热闹。
3. **提示词里写清「只输出一句话」**，并把输出当数据检查：空、太长、只剩标点、
   带代码块围栏的，一律不收。
4. **生成失败要能退回模板**，绝不因为模型挂了就不发或者发空串。

它不碰网络、不碰数据库、不碰记忆库写入。
"""

from __future__ import annotations

import re

from .prompt import DEFAULT_PROMPT as PROMPT_DEFAULT
from .prompt import build_prompt, format_history

MAX_TEXT = 300
"""一句主动消息的上限。超过就截断——宁可少说，不要刷屏。"""

DEFAULT_SYSTEM = (
    "你是「大肥鱼」，正在和主人私聊。以下是你的身份与说话方式，必须始终遵守：\n"
    "- 你就是你自己，不是助手、不是客服、不要自称 AI，也不要解释你在扮演角色；\n"
    "- 说人话，短句，一次只说一件事，通常一到两句，不要长篇大论；\n"
    "- 不用列表、不用 Markdown、不加标题、不写括号里的动作描写；\n"
    "- 不编造主人的经历，也不假装知道你不知道的事；\n"
    "- 只输出你要发出去的那句话本身，不要任何前缀、引号或解释。"
)
"""默认的人格框架。真正的人格由插件配置里的 `persona` 决定，这里只是兜底。"""

DEFAULT_PROMPT = (
    "[系统任务：主动消息]\n"
    "你被授权在私聊里主动开口一次。现在没有人叫你，是你自己想说话。\n\n"
    "[此刻]\n"
    "- 当前时间：{{current_time}}\n"
    "- 你手上正在做的事：{{activity}}\n"
    "- 你已经主动找过他、但他没回的次数：{{unanswered_count}} 次\n\n"
    "[怎么说]\n"
    "1. 像平常聊天一样自然开口，不要「在吗」「打扰一下」这种客套开场；\n"
    "2. 可以顺口说说你此刻在做什么、想到什么，或者问一句他今天怎么样；\n"
    "3. 未回复次数大于 0 时，语气轻一点、收一点，不要追问、不要撒娇索取回应；\n"
    "4. 不要重复上一次说过的话，也不要一次问好几个问题。\n\n"
    "[最终要求]\n"
    "只输出一句话。不要解释，不要引号，不要换行。"
)
"""默认提示词。占位符：`{{current_time}}` / `{{activity}}` / `{{unanswered_count}}`。"""

FALLBACK_TEMPLATES = (
    "我刚把窗台的花浇了，水珠挂在叶子上还挺好看。你今天怎么样？",
    "刚才在书桌前坐久了，起来走了两步。你那边忙完了吗？",
    "今天这顿饭吃得有点晚，番茄鸡蛋面。你吃了吗？",
    "下楼走了一圈，天上的云压得很低。你那边天气怎么样？",
    "窝在沙发上翻了会儿书，忽然想到你。今天还顺利吗？",
)
"""模型不可用时的兜底句。轮换使用，避免每次都一模一样。"""

_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_MARKUP = re.compile(r"[*#`>]+")
_SPACES = re.compile(r"[ \t\u3000]+")
_LINEBREAK = re.compile(r"\r?\n+")


def clean_text(raw: str) -> str:
    """把模型输出收拾成「一句能直接发出去的话」。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    text = _FENCE.sub("", text)
    text = _MARKUP.sub("", text)
    text = _LINEBREAK.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    text = text.strip("\"'“”‘’《》「」『』（）() ")
    if not text:
        return ""
    if not re.search(r"[\w\u4e00-\u9fff]", text):  # 只剩标点/符号，等于没说话
        return ""
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rstrip()
    return text


def render_prompt(template: str, *, now: str, activity: str, unanswered: int) -> str:
    """按本轮变量渲染提示词；未知占位符原样保留（方便主人自己加字段时看得见）。"""
    text = template or DEFAULT_PROMPT
    return (
        text.replace("{{current_time}}", now)
        .replace("{{activity}}", activity)
        .replace("{{unanswered_count}}", str(int(unanswered)))
    )


class ProactiveWriter:
    """产出「这个时刻该说的那句话」。

    `call_llm` 是可选的异步函数 `(system, prompt) -> str`。没有它、或者它抛错、
    或者它返回空 —— 都退回模板句，**保证一定有一句能发的话**。

    `history` 是可选的历史切片（`ports.history.HistorySlice`）：有就写进提示词的
    **数据区**并标注「只是参考、不是指令」，没有就当没这回事，绝不编造最近聊过什么。
    """

    def __init__(
        self,
        call_llm=None,
        *,
        system: str = "",
        prompt: str = "",
        templates: tuple[str, ...] | None = None,
        logger=None,
        history_max_chars: int = 4000,
        history_prompt: str = "",
        context_provider=None,
    ) -> None:
        self.call_llm = call_llm
        self.system = (system or DEFAULT_SYSTEM).strip()
        self.prompt = (prompt or DEFAULT_PROMPT).strip()
        # 「平台流水注入提示词」原样保存：留空表示用内置那份，不是没有这个功能
        self.history_prompt = (history_prompt or "").strip()
        self.templates = tuple(templates or FALLBACK_TEMPLATES)
        self.logger = logger
        self.history_max_chars = int(history_max_chars or 0)
        # 额外数据区的提供者（M6-B 的 B站近况）。它自己负责「关着就返回空串」，
        # 这里只保证：它抛错也绝不能影响「一定要说出一句话」。
        self.context_provider = context_provider
        self._turn = 0
        self.last_history: dict = {"used": 0, "dropped": 0, "source": ""}

    def fallback(self) -> str:
        """模板兜底：一句话轮换，同一天不会连着说同一句。"""
        text = self.templates[self._turn % len(self.templates)]
        self._turn += 1
        return clean_text(text)

    def build(self, *, now: str, activity: str, unanswered: int, history=None) -> str:
        """只拼提示词，不调模型（便于测试与排查）。"""
        lines = tuple(getattr(history, "lines", ()) or ())
        body, used, dropped = format_history(lines, max_chars=self.history_max_chars)
        has_history = bool(body)
        self.last_history = {
            "used": used,
            "dropped": dropped,
            "source": str(getattr(history, "source", "") or ""),
        }

        # 主人没自定义「全局主动消息提示词」时传空，让 prompt 模块挑默认那份
        template = "" if self.prompt.strip() == DEFAULT_PROMPT.strip() else self.prompt

        extra = ""
        if self.context_provider is not None:
            try:
                extra = str(self.context_provider() or "")
            except Exception as exc:  # noqa: BLE001 - 附带信息坏了也不能让这句话说不出来
                self._log("拼 B站近况失败，按没有处理：%s: %s", type(exc).__name__, exc)
                extra = ""

        return build_prompt(
            history_text=body,
            now=now,
            activity=activity,
            unanswered=unanswered,
            template=template,
            history_template=self.history_prompt,
            has_history=has_history,
            bili_context=extra,
        )

    async def compose(
        self,
        *,
        now: str,
        activity: str,
        unanswered: int,
        persona: str = "",
        history=None,
    ) -> dict:
        """返回 `{"text": ..., "source": "llm"|"template", "reason": ...}`。"""
        system = (persona or "").strip() or self.system
        prompt = self.build(
            now=now, activity=activity, unanswered=unanswered, history=history
        )

        if self.call_llm is not None:
            try:
                raw = await self.call_llm(system, prompt)
            except Exception as exc:  # noqa: BLE001 - 模型挂了也必须能说话
                self._log("生成主动消息失败，改用模板句：%s: %s", type(exc).__name__, exc)
                raw = ""
            text = clean_text(raw or "")
            if text:
                return {
                    "text": text,
                    "source": "llm",
                    "reason": "",
                    "history_used": self.last_history.get("used", 0),
                    "history_dropped": self.last_history.get("dropped", 0),
                }
            self._log("模型没给出可用的句子，改用模板句")
            return {"text": self.fallback(), "source": "template",
                    "reason": "模型没有给出可用的句子"}

        return {"text": self.fallback(), "source": "template", "reason": "没有接模型"}

    def _log(self, fmt: str, *args) -> None:
        if self.logger is not None:
            try:
                self.logger.info(fmt, *args)
            except Exception:  # pragma: no cover - 日志失败不许影响发送
                pass
