from typing import Any, Dict, List
import numpy as np
import pandas as pd

from agents.base_agent import BaseAgent
from core.models import AgentReport


class MoneyFlowAgent(BaseAgent):
    agent_id = "money_flow"
    agent_name_fa = "ایجنت جریان ورود و خروج پول و مشتقات"
    category_fa = "خالص جریان پول دلاری، CVD، MFI، Open Interest و Funding"

    @staticmethod
    def _estimate_usd_flow(df: pd.DataFrame, bars: int) -> tuple[float, float, float]:
        """Estimate buy/sell pressure from close location inside each candle.

        This is explicitly an OHLCV proxy, not exchange-reported taker flow. Returning
        buy, sell and net separately prevents the UI from presenting a net proxy as an
        exact on-chain/exchange cash-flow measurement.
        """
        sub = df.tail(bars)
        rng = (sub["high"] - sub["low"]).replace(0, 1e-9)
        buy_ratio = ((sub["close"] - sub["low"]) / rng).clip(0.0, 1.0)
        qvol = sub["quote_volume"] if "quote_volume" in sub else (sub["volume"] * sub["close"])
        buy_usd = float((qvol * buy_ratio).sum())
        sell_usd = float((qvol * (1.0 - buy_ratio)).sum())
        return buy_usd, sell_usd, buy_usd - sell_usd

    async def analyze(self, symbol: str, context: Dict[str, Any]) -> AgentReport:
        df: pd.DataFrame = context["df_primary"]
        df_ltf: pd.DataFrame = context.get("df_ltf", df)
        deriv: Dict[str, Any] = context["derivatives"]

        close = df["close"]
        high = df["high"]
        low = df["low"]
        vol = df["quote_volume"] if "quote_volume" in df else df["volume"] * df["close"]

        # 1. Money Flow Index (MFI 14)
        tp = (high + low + close) / 3.0
        raw_mf = tp * vol
        pos_mf = raw_mf.where(tp > tp.shift(1), 0.0).rolling(14).sum()
        neg_mf = raw_mf.where(tp < tp.shift(1), 0.0).rolling(14).sum()
        mfi = float((100.0 - (100.0 / (1.0 + (pos_mf / (neg_mf + 1e-9))))).iloc[-1])

        # 2. On-Balance Volume (OBV) Trend (Slope over 12 bars vs 12 bars prior)
        direction_sign = np.sign(close.diff().fillna(0.0))
        obv = (direction_sign * vol).cumsum()
        obv_ema_fast = float(obv.ewm(span=6, adjust=False).mean().iloc[-1])
        obv_ema_slow = float(obv.ewm(span=18, adjust=False).mean().iloc[-1])
        obv_bullish = obv_ema_fast > obv_ema_slow

        # 3. Rolling Institutional VWAP (24 bars)
        vwap_24 = float((tp.tail(24) * vol.tail(24)).sum() / max(vol.tail(24).sum(), 1e-9))
        curr_px = float(close.iloc[-1])
        vwap_diff_pct = ((curr_px - vwap_24) / max(vwap_24, 1e-9)) * 100.0

        # 4. Explicit Dollar Net Inflow / Outflow (15m, 1h, and 4-bar window)
        buy_15m_usd, sell_15m_usd, net_15m_usd = self._estimate_usd_flow(df_ltf, 1)
        buy_1h_usd, sell_1h_usd, net_1h_usd = self._estimate_usd_flow(df_ltf, 4)  # 4 x 15m
        buy_4h_usd, sell_4h_usd, net_4h_usd = self._estimate_usd_flow(df, 4)

        rng = (high - low).replace(0, 1e-9)
        buy_ratio = (close - low) / rng
        signed_vol = vol * (2.0 * buy_ratio - 1.0)
        cvd_recent_6 = float(signed_vol.tail(6).sum())
        vol_recent_6 = float(vol.tail(6).sum())
        cvd_norm = (cvd_recent_6 / max(vol_recent_6, 1e-9)) * 100.0

        vol_avg_20 = float(vol.iloc[-22:-2].mean())
        vol_surge = float(vol.iloc[-2] / max(vol_avg_20, 1e-9))

        # 5. Cross-Exchange Futures Metrics (OI, Funding, Orderbook Imbalance)
        total_oi_usd = float(deriv.get("total_open_interest_usd", 0.0))
        funding_8h = float(deriv.get("avg_funding_8h", 0.0))
        funding_pct = funding_8h * 100.0
        premium_pct = float(deriv.get("basis_premium", 0.0)) * 100.0
        ob = deriv.get("orderbook", {})
        ob_imbalance = float(ob.get("imbalance", 0.0))
        ob_net_usd = float(ob.get("bid_usd", 0.0)) - float(ob.get("ask_usd", 0.0))

        score = 0.0
        bullets: List[str] = []

        # CVD & Net Dollar Flow scoring
        score += float(np.clip(cvd_norm * 0.85, -30.0, 30.0))
        score += 10.0 if obv_bullish else -10.0

        flow_1h_tag = "ورود خالص پول 🟢" if net_1h_usd >= 0 else "خروج خالص پول 🔴"
        flow_15m_tag = "ورود" if net_15m_usd >= 0 else "خروج"
        bullets.append(
            f"برآورد {flow_1h_tag}: در ۱ ساعت اخیر {net_1h_usd/1e6:+,.2f}M$ و در آخرین کندل بسته‌شده ۱۵ دقیقه‌ای {net_15m_usd/1e6:+,.2f}M$ ({flow_15m_tag})؛ این ارقام پروکسی OHLCV هستند، نه جریان تیک‌به‌تیک (روند OBV: {'صعودی' if obv_bullish else 'نزولی'})."
        )

        # VWAP institutional benchmark
        if vwap_diff_pct > 0.15:
            score += 14.0
            bullets.append(
                f"قیمت بالای میانگین حجمی نهادی VWAP (${self.fmt_px(vwap_24)}) قرار دارد ({vwap_diff_pct:+.2f}%)؛ برتری دست خریداران است."
            )
        elif vwap_diff_pct < -0.15:
            score -= 14.0
            bullets.append(
                f"قیمت زیر میانگین حجمی نهادی VWAP (${self.fmt_px(vwap_24)}) معامله می‌شود ({vwap_diff_pct:+.2f}%)؛ غلبه فشار عرضه."
            )

        # MFI scoring
        if 54 <= mfi <= 78:
            score += 14.0
        elif mfi > 83:
            score -= 10.0
            bullets.append(f"شاخص جریان پول MFI ({mfi:.1f}) در اشباع خرید است.")
        elif 22 <= mfi <= 46:
            score -= 14.0
        elif mfi < 18:
            score += 10.0
            bullets.append(f"شاخص جریان پول MFI ({mfi:.1f}) در اشباع فروش است (پتانسیل چرخش ورود پول).")

        # Orderbook L2 Imbalance
        score += ob_imbalance * 20.0
        if abs(ob_imbalance) >= 0.15:
            side_fa = "خرید (Bid)" if ob_imbalance > 0 else "فروش (Ask)"
            bullets.append(
                f"اردربوک زنده: برتری سفارشات {side_fa} با خالص {ob_net_usd/1e3:+,.0f}K$ (عدم تعادل: {ob_imbalance*100:+.1f}%)."
            )

        # Funding Rate & Squeeze Dynamics
        if funding_pct < -0.005 and cvd_norm > 0:
            score += 18.0
            bullets.append(
                f"فاندینگ منفی ({funding_pct:+.4f}%) همزمان با ورود پول؛ پتانسیل بالای شورت اسکوییز (Short Squeeze)!"
            )
        elif funding_pct > 0.045:
            score -= 16.0
            bullets.append(
                f"هشدار ازدحام لانگ‌ها: فاندینگ بالا ({funding_pct:+.4f}%) ریسک اصلاح لانگ‌های اهرمی را زیاد کرده است."
            )
        else:
            bullets.append(
                f"مجموع Open Interest صرافی‌ها: ${total_oi_usd/1e6:,.1f}M | فاندینگ ۸ ساعته: {funding_pct:+.4f}% | MFI: {mfi:.1f}."
            )

        if vol_surge > 1.45:
            score *= 1.15

        score = round(self.clamp(score), 1)
        direction = self.score_to_direction(score, threshold=15.0)
        confidence = round(min(95.0, 50.0 + abs(score) * 0.45), 1)

        if direction == "LONG":
            summary = f"ورود خالص پول (۱ ساعته: {net_1h_usd/1e6:+.2f}M$ | MFI={mfi:.0f} | OBV صعودی) به نفع LONG (امتیاز {score:+.0f})."
        elif direction == "SHORT":
            summary = f"خروج خالص پول (۱ ساعته: {net_1h_usd/1e6:+.2f}M$ | MFI={mfi:.0f} | OBV نزولی) به نفع SHORT (امتیاز {score:+.0f})."
        else:
            summary = f"تعادل نسبی ورود و خروج پول (۱ ساعته: {net_1h_usd/1e6:+.2f}M$ | MFI={mfi:.0f} | امتیاز {score:+.0f})."

        return AgentReport(
            agent_id=self.agent_id,
            agent_name_fa=self.agent_name_fa,
            category_fa=self.category_fa,
            symbol=symbol,
            score=score,
            direction=direction,
            confidence=confidence,
            summary_fa=summary,
            bullets_fa=bullets[:4],
            metrics={
                "estimated_buy_15m_usd": round(buy_15m_usd, 0),
                "estimated_sell_15m_usd": round(sell_15m_usd, 0),
                "net_inflow_15m_usd": round(net_15m_usd, 0),
                "estimated_buy_1h_usd": round(buy_1h_usd, 0),
                "estimated_sell_1h_usd": round(sell_1h_usd, 0),
                "net_inflow_1h_usd": round(net_1h_usd, 0),
                "estimated_buy_4h_usd": round(buy_4h_usd, 0),
                "estimated_sell_4h_usd": round(sell_4h_usd, 0),
                "net_inflow_4h_usd": round(net_4h_usd, 0),
                "flow_method": "OHLCV_CLOSE_LOCATION_ESTIMATE",
                "obv_trend": "BULLISH" if obv_bullish else "BEARISH",
                "mfi_14": round(mfi, 1),
                "vwap_24": round(vwap_24, 6),
                "vwap_diff_pct": round(vwap_diff_pct, 2),
                "cvd_norm_pct": round(cvd_norm, 1),
                "vol_surge_ratio": round(vol_surge, 2),
                "total_open_interest_usd": round(total_oi_usd, 0),
                "funding_8h_pct": round(funding_pct, 4),
                "basis_premium_pct": round(premium_pct, 4),
                "orderbook_imbalance_pct": round(ob_imbalance * 100.0, 1),
                "orderbook_net_usd": round(ob_net_usd, 0),
            },
        )
