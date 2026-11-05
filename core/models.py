from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, model_validator


SignalDirection = Literal["LONG", "SHORT", "NEUTRAL"]


class AgentReport(BaseModel):
    agent_id: str
    agent_name_fa: str
    category_fa: str
    symbol: str
    score: float = Field(..., ge=-100.0, le=100.0, description="-100 (Strong Short) to +100 (Strong Long)")
    direction: SignalDirection
    confidence: float = Field(..., ge=0.0, le=100.0, description="0 to 100")
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
    signal_source: Literal["MARKET_ANALYSIS", "COPY_TRADER"]
    confidence: float = Field(..., ge=0.0, le=100.0)
    composite_score: float = Field(..., ge=-100.0, le=100.0)
    current_price: float = Field(..., gt=0.0)
    entry_low: float = Field(..., gt=0.0)
    entry_high: float = Field(..., gt=0.0)
    stop_loss: float = Field(..., gt=0.0)
    tp1: float = Field(..., gt=0.0)
    tp2: float = Field(..., gt=0.0)
    tp3: float = Field(..., gt=0.0)
    risk_reward_tp2: float = Field(..., ge=0.0)
    recommended_leverage: int = Field(..., ge=1, le=100)
    margin_mode: Literal["ISOLATED"] = "ISOLATED"
    risk_pct: float = Field(default=1.5, gt=0.0, le=10.0)
    executive_summary_fa: str
    invalidation_note_fa: str
    ai_engine_used: str
    copied_trader_address: Optional[str] = None
    copied_trader_label: Optional[str] = None
    sr_zones_and_trendlines: Dict[str, Any] = Field(default_factory=dict)
    previous_comparison: Dict[str, Any] = Field(default_factory=dict)
    agent_reports: List[AgentReport] = Field(default_factory=list)
    telegram_sent: bool = False
    strategy_version: str = "UNVERSIONED"
    research_snapshot: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_trade_geometry(self) -> "FuturesSignal":
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low must be <= entry_high")
        if self.direction == "LONG":
            if not (self.stop_loss < self.current_price < self.tp1 < self.tp2 < self.tp3):
                raise ValueError("invalid LONG stop/target geometry")
        elif self.direction == "SHORT":
            if not (self.stop_loss > self.current_price > self.tp1 > self.tp2 > self.tp3):
                raise ValueError("invalid SHORT stop/target geometry")
        return self


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
