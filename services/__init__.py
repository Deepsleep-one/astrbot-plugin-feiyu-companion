"""服务层（M3：预算；M4：日程；M5：投递队列；M5.3：提示词与分段；M5.4：随机节奏）。"""

from .budget import Budget, BudgetDecision, BudgetPolicy
from .dispatcher import DeliveryQueue, DispatchError, DispatchReport
from .pace import Pace, pace_from_config
from .prompt import build_prompt, format_history, render
from .runner import ProactiveRunner
from .segmenter import SegmentPlan, Segmenter, segmenter_from_config
from .share_relay import ShareRelay
from .timeline import DayPlan, Timeline, parse_hhmm, parse_quiet
from .writer import ProactiveWriter

__all__ = [
    "Budget",
    "BudgetDecision",
    "BudgetPolicy",
    "DayPlan",
    "DeliveryQueue",
    "DispatchError",
    "DispatchReport",
    "Pace",
    "ProactiveRunner",
    "ProactiveWriter",
    "SegmentPlan",
    "Segmenter",
    "ShareRelay",
    "pace_from_config",
    "Timeline",
    "build_prompt",
    "format_history",
    "parse_hhmm",
    "parse_quiet",
    "render",
    "segmenter_from_config",
]
