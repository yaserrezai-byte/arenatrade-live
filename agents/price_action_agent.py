from typing import Any, Dict, List
import numpy as np
import pandas as pd

from agents.base_agent import BaseAgent
from core.models import AgentReport


class PriceActionAgent(BaseAgent):
    agent_id = "price_action"
    agent_name_fa = "ایجنت تحلیل قیمت و پرایس اکشن"
    category_fa = "پرایس اکشن و کندل‌شناسی"

    async def analyze(self, symbol: str, context: Dict[str, Any]) -> AgentReport:
        df: pd.DataFrame = context["df_primary"]
        c = df["close"].values
        o = df["open"].values
        h = df["high"].values
        l = df["low"].values

        curr_price = float(c[-1])
        prev_close = float(c[-2])

        # True Range & ATR(14)
        tr = np.maximum(
            h[1:] - l[1:],
            np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])),
        )
        atr = float(pd.Series(tr).rolling(14).mean().iloc[-1])
        atr_pct = (atr / curr_price * 100.0) if curr_price > 0 else 1.0

        score = 0.0
        bullets: List[str] = []
        patterns: List[str] = []

        # Analyze last closed candle (-2) and forming candle (-1)
        for idx, tag in [(-2, "کندل تثبیت‌شده"), (-1, "کندل جاری")]:
            body = c[idx] - o[idx]
            abs_body = abs(body)
            rng = max(h[idx] - l[idx], 1e-9)
            upper_wick = h[idx] - max(c[idx], o[idx])
            lower_wick = min(c[idx], o[idx]) - l[idx]

            prev_body = c[idx - 1] - o[idx - 1]

            # Bullish Engulfing
            if prev_body < 0 and body > 0 and abs_body > abs(prev_body) * 1.15 and c[idx] >= o[idx - 1]:
                score += 24.0 if idx == -2 else 14.0
                patterns.append(f"پوشای صعودی (Bullish Engulfing - {tag})")
            # Bearish Engulfing
            elif prev_body > 0 and body < 0 and abs_body > abs(prev_body) * 1.15 and c[idx] <= o[idx - 1]:
                score -= 24.0 if idx == -2 else 14.0
                patterns.append(f"پوشای نزولی (Bearish Engulfing - {tag})")

            # Bullish Pin Bar / Hammer
            if lower_wick > abs_body * 2.1 and lower_wick / rng > 0.55:
                score += 18.0
                patterns.append(f"پین‌بار صعودی / شدو جمع‌آوری نقدینگی پایین ({tag})")
            # Bearish Pin Bar / Shooting Star
            elif upper_wick > abs_body * 2.1 and upper_wick / rng > 0.55:
                score -= 18.0
                patterns.append(f"پین‌بار نزولی / ریجکشن از بالا ({tag})")

        # Fair Value Gap (FVG / Imbalance) in recent 6 candles
        bull_fvg = None
        bear_fvg = None
        for i in range(-6, -1):
            # 3-candle FVG: candle i-1 high < candle i+1 low
            if l[i + 1] > h[i - 1] and (l[i + 1] - h[i - 1]) > 0.15 * atr:
                bull_fvg = (float(h[i - 1]), float(l[i + 1]))
            if h[i + 1] < l[i - 1] and (l[i - 1] - h[i + 1]) > 0.15 * atr:
                bear_fvg = (float(h[i + 1]), float(l[i - 1]))

        if bull_fvg and curr_price >= bull_fvg[0]:
            score += 16.0
            bullets.append(
                f"شکاف ارزش منصفانه صعودی (Bullish FVG) در محدوده {self.fmt_px(bull_fvg[0])} تا {self.fmt_px(bull_fvg[1])} به عنوان آهنربای حمایت عمل می‌کند."
            )
        if bear_fvg and curr_price <= bear_fvg[1]:
            score -= 16.0
            bullets.append(
                f"شکاف ارزش منصفانه نزولی (Bearish FVG) در محدوده {self.fmt_px(bear_fvg[0])} تا {self.fmt_px(bear_fvg[1])} فشار عرضه ایجاد کرده است."
            )

        # Liquidity Sweep (Stop Hunt) check over 20-bar high/low
        recent_20_low = float(np.min(l[-22:-2]))
        recent_20_high = float(np.max(h[-22:-2]))
        if (l[-2] < recent_20_low or l[-1] < recent_20_low) and curr_price > recent_20_low:
            score += 26.0
            bullets.append(
                f"جمع‌آوری نقدینگی کف (Liquidity Sweep زیر {self.fmt_px(recent_20_low)}) و بازگشت سریع قیمت به بالا؛ نشانه ورود اسمارت‌مانی در جهت LONG."
            )
        elif (h[-2] > recent_20_high or h[-1] > recent_20_high) and curr_price < recent_20_high:
            score -= 26.0
            bullets.append(
                f"جمع‌آوری نقدینگی سقف (Stop Hunt بالای {self.fmt_px(recent_20_high)}) و ریجکت قیمت؛ نشانه تله گاوی و برتری SHORT."
            )

        # Short-term momentum over last 5 candles relative to ATR
        move_5 = (curr_price - float(c[-6])) / max(atr, 1e-9)
        score += float(np.clip(move_5 * 11.0, -25.0, 25.0))

        # Position within 24-bar range
        hi_24 = float(np.max(h[-24:]))
        lo_24 = float(np.min(l[-24:]))
        range_pos_pct = ((curr_price - lo_24) / max(hi_24 - lo_24, 1e-9)) * 100.0

        if patterns:
            bullets.insert(0, "الگوهای کندلی شناسایی‌شده: " + " ، ".join(patterns))
        else:
            bullets.insert(
                0,
                f"مومنتوم ۵ کندل اخیر معادل {move_5:+.2f} برابر ATR است و قیمت در {range_pos_pct:.0f}% محدوده نوسانی ۲۴ کندل اخیر قرار دارد.",
            )

        bullets.append(
            f"میانگین دامنه نوسان واقعی (ATR-14): {self.fmt_px(atr)} ({atr_pct:.2f}% از قیمت) — معیار تعیین فاصله امن حد ضرر."
        )

        score = round(self.clamp(score), 1)
        direction = self.score_to_direction(score, threshold=14.0)
        confidence = round(min(94.0, 48.0 + abs(score) * 0.48), 1)

        if direction == "LONG":
            summary = f"آرایش پرایس اکشن و کندل‌ها متمایل به صعود (امتیاز {score:+.0f}) با نوسان ATR معادل {atr_pct:.2f}% است."
        elif direction == "SHORT":
            summary = f"فشار عرضه و ریجکشن‌های پرایس اکشن نزولی است (امتیاز {score:+.0f})؛ احتیاط در خرید و برتری پوزیشن شورت."
        else:
            summary = f"پرایس اکشن در فاز تعادل و فشردگی قرار دارد (امتیاز {score:+.0f})؛ نیازمند تایید شکست."

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
                "current_price": curr_price,
                "prev_close": prev_close,
                "atr": round(atr, 6),
                "atr_pct": round(atr_pct, 3),
                "range_position_pct": round(range_pos_pct, 1),
                "patterns": patterns,
            },
        )
