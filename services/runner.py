"""主动陪伴的运行时：每分钟看一眼「现在该不该说话」，然后把话真的发出去。

节奏不再是固定间隔，而是**每次说完掷一次骰子**：在 `Pace` 给的区间（默认 5–180
分钟）里抽一个数，落成 `next_due_at` 写进状态。主人一说话，这个点从「她刚才被理了」
重新算起——所以同一段对话里她不会一直插嘴，安静下来以后又会在某个不像闹钟的时间出现。

它只做三步，每一步都必须先过闸：

1. `should_speak()` 判断此刻该不该开口（免打扰 / 有没有在跑的投递 /
   **随机排的那个点到了没** / 连续未回复是否到上限 / 预算）；
2. `compose()` 让她自己写出这句话（模型不可用就退回模板句，绝不发空串）；
3. `dispatch_all_async()` 真的发（传输层自己还有一道「只发私聊」的闸）。

第 4 步是把结果记回状态：`last_sent_at` / `unanswered` / `last_tick_at`。
收到主人的消息时由 `note_incoming()` 把未回复计数清零——**这是「她有没有被理」的唯一依据**。

它不碰记忆库，也没有自己的定时线程——由插件在 AstrBot 加载后起一个 asyncio 任务，
每 `interval` 秒跑一次 `tick()`。任务在 `terminate()` 里取消。
"""

from __future__ import annotations

import asyncio
import inspect
import random
from datetime import datetime, timedelta

from ..adapters.astrbot_transport import split_session
from ..domain.context import SCENE_PRIVATE, InteractionContext
from ..domain.delivery import STATUS_PENDING, STATUS_SENDING, Delivery
from ..runtime.clock import Clock
from .dispatcher import DeliveryQueue
from .pace import Pace
from .timeline import Timeline

PING_IDEM_KEY = "ping:first"
"""上线自检那句话只发一次：账本里记了就不再发，重启也不会重复。"""

SLOT_MINUTES = 1
"""幂等键按分钟切：随机间隔最短 5 分钟，一分钟一个槽位才够用
（切 15 分钟会把 5 分钟后的第二条误判成重复而静默丢掉）。"""


class ProactiveRunner:
    def __init__(
        self,
        *,
        timeline: Timeline,
        queue: DeliveryQueue,
        transport,
        clock: Clock,
        state,
        budget=None,
        writer=None,
        interval: float = 60.0,
        startup_ping: bool = False,
        max_unanswered: int = 4,
        pace: Pace | None = None,
        rng: random.Random | None = None,
        persona: str = "",
        history=None,
        history_limit: int = 30,
        logger=None,
        # 下面三个是 v0.3 之前的老参数：现在不参与判断，留着只为旧调用不炸
        min_gap_minutes: float = 0.0,
        silence_minutes: float = 0.0,
        max_gap_minutes: float = 0.0,
    ) -> None:
        self.timeline = timeline
        self.queue = queue
        self.transport = transport
        self.clock = clock
        self.state = state
        self.budget = budget
        self.writer = writer
        self.interval = max(30.0, float(interval))
        self.startup_ping = bool(startup_ping)
        self.max_unanswered = max(0, int(max_unanswered))
        self.pace = pace or Pace()
        self.rng = rng or random.Random()
        self._legacy = {
            "min_gap_minutes": min_gap_minutes,
            "silence_minutes": silence_minutes,
            "max_gap_minutes": max_gap_minutes,
        }
        self.persona = str(persona or "")
        self.history = history
        self.history_limit = max(0, int(history_limit))
        self.logger = logger
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        # 每轮收尾后顺便做的事（M6-D 的分享交接就挂在这里）：可空。
        self.after_tick = None

    # ---- 会话 -------------------------------------------------------------
    def owner_session(self) -> str:
        """主人最近一次私聊的会话；没记录时退回配置里的 `target_session`。"""
        remembered = str(self.state.get("owner_session", "") or "")
        return remembered or str(getattr(self.transport, "default_session", "") or "")

    def remember_session(self, session: str) -> bool:
        """记下主人私聊的会话标识，格式不对就不记。"""
        if split_session(session) is None:
            return False
        if self.state.get("owner_session") == session:
            return True
        self.state.set("owner_session", session)
        return True

    def note_incoming(self, *, session: str = "", now=None) -> bool:
        """收到主人的私聊：清零未回复计数，并**重新掷**下一次主动的时间。

        真人刚被搭话时不会马上又自言自语；等聊完了，才会在某个随机的时间点想起你。
        所以这里把「下次开口」从此刻重新算起，而不是沿用之前排好的点。
        """
        moment = now or self.clock.now()
        if session:
            self.remember_session(session)
        self.state.update(
            {
                "last_incoming_at": moment.isoformat(timespec="seconds"),
                "unanswered": 0,
            }
        )
        self.schedule_next(moment, why="主人说话了")
        return True

    def context(self) -> InteractionContext | None:
        session = self.owner_session()
        parts = split_session(session)
        if parts is None:
            return None
        platform_id, _, user_id = parts
        return InteractionContext(
            platform=platform_id,
            bot_instance="",
            user_id=user_id,
            session_id=session,
            scene=SCENE_PRIVATE,
            is_owner=True,
            is_bound=True,
            now=self.clock.now().isoformat(timespec="seconds"),
        )

    # ---- 节奏 -------------------------------------------------------------
    def _moment(self, key: str) -> datetime | None:
        raw = str(self.state.get(key, "") or "")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def _elapsed(self, since: datetime | None, moment: datetime) -> float | None:
        if since is None:
            return None
        try:
            return (moment - since).total_seconds()
        except TypeError:  # 一个带时区一个不带，当作已经很久
            return None

    def _last_sent(self) -> datetime | None:
        return self._moment("last_sent_at")

    def _mark_sent(self, moment: datetime) -> None:
        self.state.update(
            {
                "last_sent_at": moment.isoformat(timespec="seconds"),
                "unanswered": int(self.state.get("unanswered", 0) or 0) + 1,
            }
        )
        self.schedule_next(moment, why="刚说完一句")

    def _anchor(self, moment: datetime) -> datetime:
        """从哪个时刻开始算「下次什么时候」。取她上次说话和主人上次说话里较晚的那个。"""
        anchors = [moment]
        for value in (self._last_sent(), self._moment("last_incoming_at")):
            if value is None:
                continue
            try:
                if value <= moment:
                    anchors.append(value)
            except TypeError:  # 一个带时区一个不带：忽略这条
                continue
        return max(anchors)

    def unanswered(self) -> int:
        try:
            return max(0, int(self.state.get("unanswered", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def schedule_next(self, moment: datetime, *, why: str = "") -> float:
        """掷一次骰子，把下一次可以开口的时刻写进状态。返回这次掷出的分钟数。"""
        gap = self.pace.gap_minutes(self.rng, at=moment)
        due = moment + timedelta(minutes=gap)
        self.state.update(
            {
                "next_due_at": due.isoformat(timespec="seconds"),
                "last_gap_minutes": round(gap, 3),
                "pace_why": why,
            }
        )
        return gap

    def due_at(self, *, now=None, create: bool = True) -> datetime | None:
        """下一次可以开口的时刻。状态里没有就按当前的锚点掷一次并记住。"""
        moment = now or self.clock.now()
        stored = self._moment("next_due_at")
        if stored is not None:
            return stored
        if not create:
            return None
        self.schedule_next(self._anchor(moment), why="首次排期")
        return self._moment("next_due_at")

    def due_left(self, *, now=None) -> float:
        """距下次可以开口还有多少秒；0 表示已经到点了。"""
        moment = now or self.clock.now()
        due = self.due_at(now=moment)
        if due is None:
            return 0.0
        try:
            return max(0.0, (due - moment).total_seconds())
        except TypeError:
            return 0.0

    def due_text(self, *, now=None) -> str:
        """给状态指令用的一句话。"""
        moment = now or self.clock.now()
        due = self.due_at(now=moment)
        if due is None:
            return "还没排下一次"
        left = self.due_left(now=moment)
        if left <= 0:
            return f"已到点（{due.strftime('%H:%M')}），下一次检查就开口"
        return f"{due.strftime('%H:%M')} 左右，还有 {int(left // 60) + 1} 分钟"

    def in_flight(self) -> int:
        """队列里还没走完的投递条数——有在跑的任务就不再新建。"""
        try:
            items = self.queue.pending()
        except Exception:  # pragma: no cover - 账本读不出来时按「没有在跑」处理
            return 0
        alive = 0
        for item in items:
            delivery = getattr(item, "delivery", item)
            status = getattr(delivery, "status", "")
            if status in (STATUS_PENDING, STATUS_SENDING):
                alive += 1
        return alive

    def should_speak(self, *, now=None) -> dict:
        """一次判断，返回 `{"ok": bool, "reason": str, "gate": str, ...}`。"""
        moment = now or self.clock.now()
        ctx = self.context()
        if ctx is None:
            return {"ok": False, "gate": "no_session",
                    "reason": "还没有主人的私聊会话，等她先说一句话"}
        if self.timeline.is_quiet(moment):
            window = getattr(self.timeline, "quiet_label", "")
            hint = f"免打扰时段（{window}）" if window else "免打扰时段"
            return {"ok": False, "gate": "quiet", "reason": f"{hint}，不打扰"}
        if self.in_flight() > 0:
            return {"ok": False, "gate": "in_flight", "reason": "已经有一条主动消息在跑了"}

        due = self.due_at(now=moment)
        left = self.due_left(now=moment)
        if left > 0:
            gap = float(self.state.get("last_gap_minutes", 0) or 0)
            return {
                "ok": False,
                "gate": "not_due",
                "reason": f"还没到这次随机出来的点（{due.strftime('%H:%M') if due else '—'}，"
                          f"这次排了 {gap:.0f} 分钟），还有 {int(left // 60) + 1} 分钟",
                "due_at": due.isoformat(timespec="seconds") if due else "",
                "gap_minutes": gap,
            }

        if self.max_unanswered > 0 and self.unanswered() >= self.max_unanswered:
            self.schedule_next(moment, why="连续没回，先忍住")
            return {"ok": False, "gate": "unanswered",
                    "reason": f"已经连着 {self.unanswered()} 条没回，这次忍住不说"}

        decision = self.budget_check()
        if not decision.allowed:
            self.schedule_next(moment, why="额度用完了")
            return {"ok": False, "gate": "budget", "reason": decision.reason,
                    "remaining_today": decision.remaining_today}
        return {
            "ok": True,
            "gate": "",
            "reason": "",
            "due_at": due.isoformat(timespec="seconds") if due else "",
            "remaining_today": decision.remaining_today,
        }

    def budget_check(self):
        """预算判断；没接预算时永远允许（纯域测试里可以省略）。"""
        if self.budget is None:
            return _Always()
        from ..domain.grants import CAP_PROACTIVE

        return self.budget.check(CAP_PROACTIVE)

    # ---- 一次检查 ---------------------------------------------------------
    def _slot_key(self, moment: datetime) -> str:
        bucket = moment.replace(second=0, microsecond=0)
        return f"proactive:{self.owner_session()}:{bucket.isoformat(timespec='minutes')}"

    async def _compose(self, moment: datetime) -> dict:
        current = self.timeline.current_activity(moment)
        activity = current.title if current is not None else "手头没什么特别的事"
        if self.writer is None:
            return {"text": "刚才在发呆，忽然想到你。今天怎么样？",
                    "source": "builtin", "reason": "没有接文案生成器"}
        history = await self._history_slice()
        return await self.writer.compose(
            now=moment.strftime("%Y-%m-%d %H:%M %A"),
            activity=activity,
            unanswered=self.unanswered(),
            persona=self.persona,
            history=history,
        )

    async def _history_slice(self):
        """读最近聊天记录；读不到就返回 None（绝不编造）。"""
        if self.history is None or self.history_limit <= 0:
            return None
        session = self.owner_session()
        if not session:
            return None
        try:
            slice_ = await self.history.recent(session, limit=self.history_limit)
        except Exception as exc:  # noqa: BLE001 - 读历史失败不该挡住说话
            self._log("读聊天记录失败，这次不带上下文：%s: %s", type(exc).__name__, exc)
            return None
        return slice_ or None

    async def tick(self, *, now=None) -> dict:
        """一次检查。返回 `{ok, planned, plan_reason, gate, report, ...}`。"""
        moment = now or self.clock.now()
        verdict = self.should_speak(now=moment)
        self.state.update({"last_tick_at": moment.isoformat(timespec="seconds")})
        if not verdict["ok"]:
            out = {
                "ok": True,
                "planned": 0,
                "plan_reason": verdict["reason"],
                "gate": verdict["gate"],
                "skipped": verdict["gate"],
                "quiet": self.timeline.is_quiet(moment),
                "report": {"sent": 0, "failed": 0, "dropped": 0, "skipped": 0,
                           "attempts": 0, "notes": []},
            }
            await self._run_after_tick(moment)
            return out

        written = await self._compose(moment)
        delivery = Delivery(
            id=self._slot_key(moment),
            session_id=self.owner_session(),
            text=str(written.get("text") or ""),
            status=STATUS_PENDING,
            idem_key=self._slot_key(moment),
            created_at=moment.isoformat(timespec="seconds"),
            extra={
                "simulated": True,
                "source": written.get("source", ""),
                "unanswered_before": self.unanswered(),
            },
        )
        self.queue.enqueue(delivery)
        report = await self.queue.dispatch_all_async(self.transport, now=moment)
        if report.sent:
            self._mark_sent(moment)
            if self.budget is not None:
                from ..domain.grants import CAP_PROACTIVE

                self.budget.consume(CAP_PROACTIVE)
        self.state.update(
            {
                "last_report": report.to_dict(),
                "last_text": delivery.text,
            }
        )
        gap_done = float(self.state.get("last_gap_minutes", 0) or 0)
        self._log(
            "主动消息：%s（来源：%s，未回复计数：%d，参考记录：%d 条%s，"
            "这次隔了 %.0f 分钟，下次排到 %s）",
            report.summary(),
            written.get("source", ""),
            self.unanswered(),
            int(written.get("history_used") or 0),
            f"，截掉 {int(written.get('history_dropped') or 0)} 条"
            if written.get("history_dropped")
            else "",
            gap_done,
            str(self.state.get("next_due_at", ""))[11:16],
        )
        await self._run_after_tick(moment)
        return {
            "ok": True,
            "planned": 1,
            "plan_reason": f"随机排的点到了，隔了 {gap_done:.0f} 分钟",
            "gate": "",
            "gap_minutes": gap_done,
            "next_due_at": str(self.state.get("next_due_at", "")),
            "quiet": self.timeline.is_quiet(moment),
            "text": delivery.text,
            "source": written.get("source", ""),
            "report": report.to_dict(),
        }

    async def _run_after_tick(self, moment) -> None:
        """跑挂在 tick 后面的收尾动作；它自己出错只记日志，不许影响这一轮。"""
        hook = self.after_tick
        if hook is None:
            return
        try:
            result = hook(moment)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - 收尾动作失败不该拖垮主动陪伴
            self._log("收尾动作失败（%s）：%s", type(exc).__name__, exc)

    async def ping_once(self) -> bool:
        """第一次上线时确认通道真的通：只发一次，之后账本记住。"""
        if not self.startup_ping or self.state.get("pinged_at"):
            return False
        session = self.owner_session()
        if split_session(session) is None:
            return False
        moment = self.clock.now()
        delivery = Delivery(
            id=PING_IDEM_KEY,
            session_id=session,
            text="主人，我上线了。消息通道通了——有事我会主动找你，不会一直刷屏。",
            status=STATUS_PENDING,
            idem_key=PING_IDEM_KEY,
            created_at=moment.isoformat(timespec="seconds"),
            extra={"simulated": False, "kind": "startup_ping"},
        )
        self.queue.enqueue(delivery)
        report = await self.queue.dispatch_all_async(self.transport, now=moment)
        if report.sent:
            self.state.set("pinged_at", moment.isoformat(timespec="seconds"))
            self._mark_sent(moment)
            self._log("上线自检消息已发出（回执：%s）", report.summary())
            return True
        self._log("上线自检消息没发出去：%s", report.summary())
        return False

    # ---- 循环 -------------------------------------------------------------
    async def run_forever(self) -> None:
        try:
            await asyncio.sleep(15)  # 让平台先连上
            await self.ping_once()
            while not self._stopping.is_set():
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 一轮出错不许把循环打死
                    self._log("这一轮主动检查出错：%s: %s", type(exc).__name__, exc)
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾不许抛
            pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _log(self, fmt: str, *args) -> None:
        if self.logger is not None:
            try:
                self.logger.info("[feiyu_companion] " + fmt, *args)
            except Exception:  # pragma: no cover
                pass


class _Always:
    """没有接预算时的放行凭据。"""

    allowed = True
    reason = ""
    retry_at = ""
    remaining_today = -1
