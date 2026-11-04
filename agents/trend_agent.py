from typing import Any, Dict, List
import numpy as np
import pandas as pd

from agents.base_agent import BaseAgent
from core.models import AgentReport


class TrendDetectionAgent(BaseAgent):
    agent_id = "trend"
    agent_name_fa = "ایجنت تشخیص روند و ساختار بازار"
    category_fa = "مولتی‌تایم‌فریم، ساختار BOS/CHoCH و قدرت روند"

    @staticmethod
    def _compute_adx(df: pd.DataFrame, period: int = 14) -> Dict[str, float]:
        high = df["high"]
        low = df["low"]
        close = df["close"]

        up_move = high.diff()
        down_move = -low.diff()

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = tr.ewm(alpha=1 / period, adjust=False).mean()
        plus_di = 100.0 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / (atr + 1e-9)
        minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / (atr + 1e-9)

        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
        adx = dx.ewm(alpha=1 / period, adjust=False).mean()
        return {
            "adx": float(adx.iloc[-1]),
            "plus_di": float(plus_di.iloc[-1]),
            "minus_di": float(minus_di.iloc[-1]),
        }

    @staticmethod
    def _compute_supertrend_dir(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> int:
        h = df["high"].values
        l = df["low"].values
        c = df["close"].values
        tr = np.zeros(len(c))
        tr[0] = h[0] - l[0]
        for i in range(1, len(c)):
            tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        atr = pd.Series(tr).rolling(period).mean().bfill().values
        hl2 = (h + l) / 2.0
        upper = hl2 + mult * atr
        lower = hl2 - mult * atr

        final_upper = upper.copy()
        final_lower = lower.copy()
        direction = 1

        for i in range(1, len(c)):
            if upper[i] < final_upper[i - 1] or c[i - 1] > final_upper[i - 1]:
                final_upper[i] = upper[i]
            else:
                final_upper[i] = final_upper[i - 1]

            if lower[i] > final_lower[i - 1] or c[i - 1] < final_lower[i - 1]:
                final_lower[i] = lower[i]
            else:
                final_lower[i] = final_lower[i - 1]

            if direction == -1 and c[i] > final_upper[i - 1]:
                direction = 1
            elif direction == 1 and c[i] < final_lower[i - 1]:
                direction = -1
        return direction

    async def analyze(self, symbol: str, context: Dict[str, Any]) -> AgentReport:
        df: pd.DataFrame = context["df_primary"]
        df_htf: pd.DataFrame = context.get("df_htf", df)
        df_ltf: pd.DataFrame = context.get("df_ltf", df)

        close = df["close"]
        curr_px = float(close.iloc[-1])

        ema9 = float(close.ewm(span=9, adjust=False).mean().iloc[-1])
        ema21 = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])

        # HTF (e.g. 4h) trend alignment
        htf_close = df_htf["close"]
        htf_ema21 = float(htf_close.ewm(span=21, adjust=False).mean().iloc[-1])
        htf_ema50 = float(htf_close.ewm(span=50, adjust=False).mean().iloc[-1])
        htf_px = float(htf_close.iloc[-1])

        # LTF (e.g. 15m) trigger alignment
        ltf_close = df_ltf["close"]
        ltf_ema9 = float(ltf_close.ewm(span=9, adjust=False).mean().iloc[-1])
        ltf_ema21 = float(ltf_close.ewm(span=21, adjust=False).mean().iloc[-1])

        adx_info = self._compute_adx(df)
        adx = adx_info["adx"]
        plus_di = adx_info["plus_di"]
        minus_di = adx_info["minus_di"]
        supertrend_dir = self._compute_supertrend_dir(df)

        # Market Structure (Swing Highs & Lows over two 18-bar segments)
        h1 = float(df["high"].iloc[-38:-20].max())
        l1 = float(df["low"].iloc[-38:-20].min())
        h2 = float(df["high"].iloc[-20:].max())
        l2 = float(df["low"].iloc[-20:].min())

        score = 0.0
        bullets: List[str] = []

        # Primary EMA Ribbon
        if curr_px > ema21 > ema50 > ema200:
            score += 30.0
            bullets.append("آرایش کامل صعودی میانگین‌های متحرک نمایی (قیمت > EMA21 > EMA50 > EMA200).")
        elif curr_px < ema21 < ema50 < ema200:
            score -= 30.0
            bullets.append("آرایش کامل نزولی میانگین‌های متحرک نمایی (قیمت < EMA21 < EMA50 < EMA200).")
        elif curr_px > ema50:
            score += 12.0
            bullets.append(f"قیمت بالای میانگین EMA50 ({self.fmt_px(ema50)}) در حال حفظ ساختار صعودی است.")
        else:
            score -= 12.0
            bullets.append(f"قیمت زیر میانگین EMA50 ({self.fmt_px(ema50)}) معامله می‌شود.")

        # HTF Trend Confirmation
        htf_bull = htf_px > htf_ema21 and htf_ema21 >= htf_ema50 * 0.998
        htf_bear = htf_px < htf_ema21 and htf_ema21 <= htf_ema50 * 1.002
        if htf_bull:
            score += 24.0
            bullets.append("روند تایم‌فریم بالاتر (HTF) صعودی بوده و پشتیبان پوزیشن‌های LONG است.")
        elif htf_bear:
            score -= 24.0
            bullets.append("روند تایم‌فریم بالاتر (HTF) نزولی است و وزن پوزیشن‌های SHORT را تقویت می‌کند.")

        # Supertrend + LTF trigger
        score += 16.0 if supertrend_dir == 1 else -16.0
        score += 8.0 if ltf_ema9 > ltf_ema21 else -8.0

        # Market Structure BOS / HH-HL
        structure_label = "RANGE"
        if h2 > h1 and l2 > l1:
            structure_label = "BULLISH_HH_HL"
            score += 18.0
            bullets.append("ساختار بازار صعودی (ثبت سقف و کف بالاتر HH/HL و شکست ساختار به بالا BOS).")
        elif h2 < h1 and l2 < l1:
            structure_label = "BEARISH_LH_LL"
            score -= 18.0
            bullets.append("ساختار بازار نزولی (ثبت سقف و کف پایین‌تر LH/LL و تداوم BOS نزولی).")
        else:
            bullets.append("ساختار بازار در فاز تراکم/تغییر کاراکتر (CHoCH / رنج) قرار دارد.")

        # ADX Trend Strength multiplier
        if adx < 18.0:
            score *= 0.72
            bullets.append(f"شاخص قدرت روند ضعیف است (ADX={adx:.1f} — بازار کم‌روند/فرسایشی).")
        elif adx >= 25.0:
            di_bias = "صعودی (+DI > -DI)" if plus_di > minus_di else "نزولی (-DI > +DI)"
            bullets.append(f"روند قدرتمند و جهت‌دار فعال است (ADX={adx:.1f}، برتری {di_bias}).")
            score += 10.0 if plus_di > minus_di else -10.0

        score = round(self.clamp(score), 1)
        direction = self.score_to_direction(score, threshold=16.0)
        confidence = round(min(96.0, 52.0 + abs(score) * 0.44), 1)

        regime_fa = "روند صعودی تاییدشده" if direction == "LONG" else ("روند نزولی تاییدشده" if direction == "SHORT" else "بازار رنج / بدون روند قاطع")
        summary = f"وضعیت ساختار بازار: {regime_fa} (ADX={adx:.1f}، سوپرترند {'صعودی' if supertrend_dir==1 else 'نزولی'}، امتیاز {score:+.0f})."

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
                "ema9": round(ema9, 6),
                "ema21": round(ema21, 6),
                "ema50": round(ema50, 6),
                "ema200": round(ema200, 6),
                "htf_bullish": htf_bull,
                "htf_bearish": htf_bear,
                "adx": round(adx, 1),
                "plus_di": round(plus_di, 1),
                "minus_di": round(minus_di, 1),
                "supertrend": "BULLISH" if supertrend_dir == 1 else "BEARISH",
                "market_structure": structure_label,
            },
        )
