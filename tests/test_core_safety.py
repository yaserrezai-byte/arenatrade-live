import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
from pydantic import ValidationError

from agents.sr_agent import SupportResistanceAgent
from core.database import Database
from core.models import FuturesSignal
from data_sources.market_data import MarketDataHub
from services.scanner_service import MarketScannerService


def valid_signal(**updates):
    data = {
        "signal_id": "T-1",
        "timestamp_iso": "2026-11-04 08:30:00 UTC",
        "symbol": "BTC",
        "pair": "BTCUSDT.P",
        "timeframe": "1h",
        "direction": "LONG",
        "actionable": True,
        "signal_source": "MARKET_ANALYSIS",
        "confidence": 75.0,
        "composite_score": 35.0,
        "current_price": 100.0,
        "entry_low": 99.5,
        "entry_high": 100.5,
        "stop_loss": 95.0,
        "tp1": 106.0,
        "tp2": 110.0,
        "tp3": 115.0,
        "risk_reward_tp2": 2.0,
        "recommended_leverage": 3,
        "executive_summary_fa": "test",
        "invalidation_note_fa": "test",
        "ai_engine_used": "test",
    }
    data.update(updates)
    return data


class SignalInvariantTests(unittest.TestCase):
    def test_valid_long_is_accepted(self):
        self.assertEqual(FuturesSignal(**valid_signal()).direction, "LONG")

    def test_invalid_long_geometry_is_rejected(self):
        with self.assertRaises(ValidationError):
            FuturesSignal(**valid_signal(stop_loss=101.0))

    def test_invalid_short_geometry_is_rejected(self):
        bad = valid_signal(
            direction="SHORT", composite_score=-40, stop_loss=105,
            tp1=94, tp2=90, tp3=85,
        )
        self.assertEqual(FuturesSignal(**bad).direction, "SHORT")
        bad["tp2"] = 97
        with self.assertRaises(ValidationError):
            FuturesSignal(**bad)

    def test_unknown_pipeline_source_is_rejected(self):
        with self.assertRaises(ValidationError):
            FuturesSignal(**valid_signal(signal_source="MIXED_PIPELINE"))


class OhlcvQualityTests(unittest.TestCase):
    def test_validation_sorts_and_deduplicates(self):
        rows = []
        for i in range(55):
            rows.append({
                "timestamp": i,
                "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0,
                "volume": 10.0, "quote_volume": 1000.0,
            })
        rows.append(dict(rows[-1]))
        df = MarketDataHub._validate_ohlcv(pd.DataFrame(list(reversed(rows))), "BTC", "1h")
        self.assertEqual(len(df), 55)
        self.assertTrue(df["timestamp"].is_monotonic_increasing)

    def test_validation_rejects_insufficient_data(self):
        df = pd.DataFrame([{
            "timestamp": 1, "open": 100, "high": 90, "low": 80, "close": 95,
            "volume": 1, "quote_volume": 95,
        }])
        with self.assertRaises(ValueError):
            MarketDataHub._validate_ohlcv(df, "BTC", "1h")


class SummaryIntegrityTests(unittest.TestCase):
    def test_summary_reads_real_metric_keys(self):
        service = MarketScannerService()
        sig = valid_signal()
        sig.update({
            "previous_comparison": {
                "has_previous": True, "price_change_pct": 1.25, "previous_score": 30.0,
                "htf_4h_regime": "BULLISH", "safety_veto_reasons": [],
            },
            "agent_reports": [
                {"agent_id": "money_flow", "metrics": {"net_inflow_1h_usd": 1_500_000}},
                {"agent_id": "indicators", "metrics": {"rsi_14": 57.4, "ma_status_fa": "بالای MA50"}},
                {"agent_id": "support_resistance", "metrics": {"s1": 95.0, "r1": 110.0}},
            ],
        })
        text = service._format_auto_scan_report_html([{"signal": sig}], ["BTC"])
        self.assertIn("+1.50M USD", text)
        self.assertIn("57.4", text)
        self.assertIn("S1:<code>95", text)
        self.assertIn("Δقیمت:<code>+1.25%", text)
        self.assertIn("روند 4H: <b>صعودی", text)


class FibonacciTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def frame(up: bool, n: int = 210):
        close = np.linspace(100.0, 150.0, n) if up else np.linspace(150.0, 100.0, n)
        return pd.DataFrame({
            "timestamp": np.arange(n) * 3_600_000,
            "open": close - (0.2 if up else -0.2),
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(n, 100.0),
            "quote_volume": np.full(n, 100_000.0),
        })

    async def test_bullish_fibonacci_geometry(self):
        df = self.frame(True)
        rep = await SupportResistanceAgent().analyze(
            "BTC", {"df_primary": df, "df_htf": df.tail(160).reset_index(drop=True), "derivatives": {}}
        )
        fib = rep.metrics["fibonacci"]
        self.assertEqual(fib["impulse_direction"], "BULLISH")
        self.assertGreater(fib["levels"]["23.6%"], fib["levels"]["78.6%"])
        self.assertGreater(fib["extension_1.618"], fib["swing_high"])

    async def test_bearish_fibonacci_geometry(self):
        df = self.frame(False)
        rep = await SupportResistanceAgent().analyze(
            "BTC", {"df_primary": df, "df_htf": df.tail(160).reset_index(drop=True), "derivatives": {}}
        )
        fib = rep.metrics["fibonacci"]
        self.assertEqual(fib["impulse_direction"], "BEARISH")
        self.assertLess(fib["levels"]["23.6%"], fib["levels"]["78.6%"])
        self.assertLess(fib["extension_1.618"], fib["swing_low"])


class PendingDeliveryTests(unittest.TestCase):
    def test_pending_copy_signal_is_deduplicated_by_whale_position(self):
        with tempfile.TemporaryDirectory() as td:
            db = Database(db_path=Path(td) / "test.db")
            sig = valid_signal(
                signal_id="COPY-1", signal_source="COPY_TRADER",
                copied_trader_address="0xabc", symbol="SOL",
            )
            db.save_signal(sig)
            pending = db.get_pending_copy_signal("0xabc", "SOL", "LONG")
            self.assertIsNotNone(pending)
            self.assertEqual(pending["signal_id"], "COPY-1")
            self.assertIsNone(db.get_pending_copy_signal("0xother", "SOL", "LONG"))


if __name__ == "__main__":
    unittest.main()
