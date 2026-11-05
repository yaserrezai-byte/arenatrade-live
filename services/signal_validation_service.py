"""Leak-resistant forward outcome tracker for frozen Market Analysis versions.

Policy:
- T+1 execution: evaluation starts on the first fully closed 15m candle that opens
  after the signal timestamp. This avoids using pre-signal movement from the same bar.
- Entry must be touched within VALIDATION_ENTRY_WINDOW_BARS.
- TP2, SL and a vertical time barrier form a triple-barrier outcome.
- If TP2 and SL are both touched in one candle, SL wins (pessimistic ambiguity policy).
- Net R subtracts the configured round-trip cost estimate.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Dict, List, Optional

from core.config import settings
from core.database import db
from data_sources.market_data import market_hub


INTERVAL_MS = 900_000


class SignalValidationService:
    def __init__(self) -> None:
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.last_run_ts: int = 0
        self.last_error: Optional[str] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    @staticmethod
    def _first_evaluation_bar_ms(signal_created_at: int) -> int:
        signal_ms = int(signal_created_at) * 1000
        return int(math.ceil(signal_ms / INTERVAL_MS) * INTERVAL_MS)

    @staticmethod
    def _entry_touched(signal: Dict[str, Any], bar: Dict[str, float]) -> bool:
        return float(bar["low"]) <= float(signal["entry_high"]) and float(bar["high"]) >= float(signal["entry_low"])

    @staticmethod
    def _conservative_entry(signal: Dict[str, Any], bar: Dict[str, float]) -> float:
        if signal["direction"] == "LONG":
            return max(float(signal["entry_low"]), min(float(signal["entry_high"]), float(bar["high"])))
        return min(float(signal["entry_high"]), max(float(signal["entry_low"]), float(bar["low"])))

    @staticmethod
    def _metrics(
        direction: str,
        entry: float,
        stop: float,
        bars: List[Dict[str, float]],
        cost_pct: float,
        exit_price: Optional[float] = None,
    ) -> Dict[str, float]:
        risk = max(abs(entry - stop), 1e-12)
        if direction == "LONG":
            mfe_r = (max(float(b["high"]) for b in bars) - entry) / risk
            mae_r = (min(float(b["low"]) for b in bars) - entry) / risk
            signed_pnl = ((exit_price - entry) if exit_price is not None else 0.0)
        else:
            mfe_r = (entry - min(float(b["low"]) for b in bars)) / risk
            mae_r = (entry - max(float(b["high"]) for b in bars)) / risk
            signed_pnl = ((entry - exit_price) if exit_price is not None else 0.0)
        gross_r = signed_pnl / risk if exit_price is not None else 0.0
        cost_r = (entry * cost_pct / 100.0) / risk
        return {
            "gross_r": round(gross_r, 4),
            "net_r": round(gross_r - cost_r, 4) if exit_price is not None else 0.0,
            "mae_r": round(mae_r, 4),
            "mfe_r": round(mfe_r, 4),
        }

    async def evaluate_one(self, row: Dict[str, Any]) -> Dict[str, Any]:
        signal = row["signal"]
        symbol = signal["symbol"]
        entry_window = max(1, int(settings.get("VALIDATION_ENTRY_WINDOW_BARS", 8)))
        max_hold = max(4, int(settings.get("VALIDATION_MAX_HOLD_BARS", 96)))
        cost_pct = float(row.get("estimated_cost_pct") or settings.get("ESTIMATED_ROUND_TRIP_COST_PCT", 0.18))

        df = await market_hub.fetch_ohlcv(symbol, "15m", min(300, entry_window + max_hold + 20))
        candles = [
            {
                "timestamp": int(r["timestamp"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            }
            for _, r in df.iterrows()
        ]
        first_bar_ms = self._first_evaluation_bar_ms(int(row["signal_created_at"]))
        eligible = [b for b in candles if b["timestamp"] >= first_bar_ms]
        if not eligible:
            return {"status": row["status"], "changed": False}

        entry_at = int(row.get("entry_at") or 0)
        entry_price = float(row.get("entry_price") or 0.0)
        entry_idx: Optional[int] = None

        if row["status"] == "PENDING_ENTRY":
            for idx, bar in enumerate(eligible[:entry_window]):
                if self._entry_touched(signal, bar):
                    entry_idx = idx
                    entry_at = int(bar["timestamp"] // 1000)
                    entry_price = self._conservative_entry(signal, bar)
                    break
            if entry_idx is None:
                if len(eligible) >= entry_window:
                    db.update_signal_outcome(
                        row["signal_id"],
                        status="NO_ENTRY",
                        result="NO_ENTRY",
                        bars_observed=entry_window,
                        exit_at=int(eligible[entry_window - 1]["timestamp"] // 1000),
                        details_json={
                            "execution_policy": "T_PLUS_1_CONFIRMED_15M",
                            "entry_window_bars": entry_window,
                            "reason": "entry_range_not_touched",
                        },
                    )
                    return {"status": "NO_ENTRY", "changed": True}
                return {"status": "PENDING_ENTRY", "changed": False}
        else:
            for idx, bar in enumerate(eligible):
                if int(bar["timestamp"] // 1000) >= entry_at:
                    entry_idx = idx
                    break
            if entry_idx is None:
                return {"status": "OPEN", "changed": False}

        path = eligible[entry_idx : entry_idx + max_hold]
        stop = float(signal["stop_loss"])
        target = float(signal["tp2"])
        direction = signal["direction"]
        ambiguous = False
        exit_price: Optional[float] = None
        exit_at: Optional[int] = None
        result: Optional[str] = None
        observed: List[Dict[str, float]] = []

        for bar in path:
            observed.append(bar)
            if direction == "LONG":
                hit_sl = bar["low"] <= stop
                hit_tp = bar["high"] >= target
            else:
                hit_sl = bar["high"] >= stop
                hit_tp = bar["low"] <= target

            if hit_sl and hit_tp:
                # Intrabar ordering is unknowable from OHLC; pessimistic policy prevents inflated results.
                ambiguous = True
                result, exit_price = "SL", stop
            elif hit_sl:
                result, exit_price = "SL", stop
            elif hit_tp:
                result, exit_price = "TP2", target

            if result:
                exit_at = int(bar["timestamp"] // 1000)
                break

        if result is None and len(path) >= max_hold:
            result = "TIMEOUT"
            exit_price = float(path[max_hold - 1]["close"])
            exit_at = int(path[max_hold - 1]["timestamp"] // 1000)

        if result is not None and exit_price is not None and exit_at is not None:
            metrics = self._metrics(direction, entry_price, stop, observed, cost_pct, exit_price)
            final_status = "TIMEOUT" if result == "TIMEOUT" else "CLOSED"
            db.update_signal_outcome(
                row["signal_id"],
                status=final_status,
                entry_price=entry_price,
                entry_at=entry_at,
                exit_price=exit_price,
                exit_at=exit_at,
                result=result,
                bars_observed=len(observed),
                ambiguous_bar=1 if ambiguous else 0,
                details_json={
                    "execution_policy": "T_PLUS_1_CONFIRMED_15M",
                    "barrier_policy": "SL_FIRST_IF_SAME_BAR",
                    "target_barrier": "TP2",
                    "entry_window_bars": entry_window,
                    "max_hold_bars": max_hold,
                },
                **metrics,
            )
            return {"status": final_status, "result": result, "changed": True, **metrics}

        metrics = self._metrics(direction, entry_price, stop, observed, cost_pct)
        db.update_signal_outcome(
            row["signal_id"],
            status="OPEN",
            entry_price=entry_price,
            entry_at=entry_at,
            bars_observed=len(observed),
            mae_r=metrics["mae_r"],
            mfe_r=metrics["mfe_r"],
            details_json={
                "execution_policy": "T_PLUS_1_CONFIRMED_15M",
                "barrier_policy": "SL_FIRST_IF_SAME_BAR",
                "target_barrier": "TP2",
                "entry_window_bars": entry_window,
                "max_hold_bars": max_hold,
            },
        )
        return {"status": "OPEN", "changed": row["status"] != "OPEN"}

    async def evaluate_all_once(self) -> Dict[str, Any]:
        rows = db.list_open_signal_outcomes(limit=250)
        sem = asyncio.Semaphore(4)
        results = []

        async def run(row: Dict[str, Any]) -> None:
            async with sem:
                try:
                    results.append(await self.evaluate_one(row))
                except Exception as exc:
                    results.append({"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"})

        await asyncio.gather(*(run(row) for row in rows))
        self.last_run_ts = int(time.time())
        errors = [r for r in results if r.get("status") == "ERROR"]
        self.last_error = errors[0]["error"] if errors else None
        return {
            "checked": len(rows),
            "changed": sum(1 for r in results if r.get("changed")),
            "errors": len(errors),
        }

    async def _loop(self) -> None:
        await asyncio.sleep(25)
        while self._running:
            try:
                await self.evaluate_all_once()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(300)


signal_validation_service = SignalValidationService()
