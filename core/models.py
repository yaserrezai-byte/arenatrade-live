from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


SignalDirection = Literal["LONG", "SHORT", "NEUTRAL"]


class AgentReport(BaseModel):
    agent_id: str
    agent_name_fa: str
    category_fa: str
    symbol: str
    score: float = Field(..., description="-100 (Strong Short) to +100 (Strong Long)")
    direction: SignalDirection
    confidence: float = Field(..., description="0 to 100")
    summary_fa: str
    bullets_fa: List[str] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    veto: bool = False
    veto_reason_fa: Optional[str] = None


class FuturesSignal(BaseModel):
    signal_id: str
    timestamp_iso: str
    symbol: str
    pair: str
    timeframe: str
    direction: SignalDirection
    actionable: bool
    signal_source: str  # "MULTI_AGENT_CONFLUENCE" | "SMART_COPY_CONFIRMED" | "COPY_DIRECT"
    confidence: float
    composite_score: float
    current_price: float
    entry_low: float
    entry_high: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    risk_reward_tp2: float
    recommended_leverage: int
    margin_mode: str = "ISOLATED"
    risk_pct: float = 1.5
    executive_summary_fa: str
    invalidation_note_fa: str
    ai_engine_used: str
    copied_trader_address: Optional[str] = None
    copied_trader_label: Optional[str] = None
    sr_zones_and_trendlines: Dict[str, Any] = Field(default_factory=dict)
    previous_comparison: Dict[str, Any] = Field(default_factory=dict)
    agent_reports: List[AgentReport] = Field(default_factory=list)
    telegram_sent: bool = False


class TraderPosition(BaseModel):
    coin: str
    direction: Literal["LONG", "SHORT"]
    size: float
    entry_price: float
    position_value_usd: float
    unrealized_pnl_usd: float
    roe_pct: float
    leverage: int
    liquidation_price: Optional[float] = None


class TraderProfile(BaseModel):
    address: str
    label: str
    rank: int
    status: Literal["ACTIVE", "STANDBY", "ROTATED_OUT"]
    account_value_usd: float
    pnl_day_usd: float
    pnl_week_usd: float
    pnl_month_usd: float
    roi_month_pct: float
    win_rate_pct: float
    smart_score: float
    consecutive_losses: int = 0
    max_allowed_losses: int = 2
    copied_wins: int = 0
    copied_losses: int = 0
    last_checked_ts: int = 0
    open_positions: List[TraderPosition] = Field(default_factory=list)
    recent_closed_trades: List[Dict[str, Any]] = Field(default_factory=list)
