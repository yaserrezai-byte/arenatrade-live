from abc import ABC, abstractmethod
from typing import Any, Dict

from core.models import AgentReport, SignalDirection


class BaseAgent(ABC):
    agent_id: str = "base"
    agent_name_fa: str = "ایجنت پایه"
    category_fa: str = "عمومی"

    @staticmethod
    def clamp(val: float, low: float = -100.0, high: float = 100.0) -> float:
        return max(low, min(high, float(val)))

    @staticmethod
    def fmt_px(px: float) -> str:
        val = float(px)
        if abs(val) >= 1000:
            return f"{val:,.2f}"
        if abs(val) >= 1:
            return f"{val:,.4f}".rstrip("0").rstrip(".")
        return f"{val:.6f}".rstrip("0").rstrip(".")

    @staticmethod
    def score_to_direction(score: float, threshold: float = 15.0) -> SignalDirection:
        if score >= threshold:
            return "LONG"
        if score <= -threshold:
            return "SHORT"
        return "NEUTRAL"

    @abstractmethod
    async def analyze(self, symbol: str, context: Dict[str, Any]) -> AgentReport:
        raise NotImplementedError
