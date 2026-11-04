from typing import Any, Dict, List
import pandas as pd

from agents.base_agent import BaseAgent
from core.models import AgentReport


class TechnicalIndicatorAgent(BaseAgent):
    agent_id = "indicators"
    agent_name_fa = "ایجنت اندیکاتورها (RSI, MA/EMA, MACD, ایچیموکو)"
    category_fa = "RSI مولتی‌تایم‌فریم، میانگین‌های متحرک MA/EMA، مکدی و واگرایی"

    @staticmethod
    def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0.0).ewm(alpha=1 / period, adjust=False).mean()
        loss = (-delta.clip(upper=0.0)).ewm(alpha=1 / period, adjust=False).mean()
        rs = gain / (loss + 1e-9)
        return 100.0 - (100.0 / (1.0 + rs))

    async def analyze(self, symbol: str, context: Dict[str, Any]) -> AgentReport:
        df: pd.DataFrame = context["df_primary"]
        df_ltf: pd.DataFrame = context.get("df_ltf", df)
        df_htf: pd.DataFrame = context.get("df_htf", df)

        close = df["close"]
        high = df["high"]
        low = df["low"]
        curr_px = float(close.iloc[-1])

        # 1. Multi-Timeframe RSI (14) on 15m, Primary (1h), and HTF (4h)
        rsi_series = self._calc_rsi(close, 14)
        rsi = float(rsi_series.iloc[-1])
        rsi_prev = float(rsi_series.iloc[-2])
        rsi_ltf = float(self._calc_rsi(df_ltf["close"], 14).iloc[-1])
        rsi_htf = float(self._calc_rsi(df_htf["close"], 14).iloc[-1])

        # 2. Moving Averages: SMA (MA20, MA50, MA200) & EMA (EMA21, EMA50, EMA200)
        ma20_s = close.rolling(20).mean()
        ma50_s = close.rolling(50).mean()
        ma200_s = close.rolling(200).mean() if len(close) >= 200 else close.expanding().mean()

        ma20 = float(ma20_s.iloc[-1])
        ma50 = float(ma50_s.iloc[-1])
        ma200 = float(ma200_s.iloc[-1])

        ema21 = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])

        # Detect recent MA20 / MA50 Golden or Death Cross
        ma_golden_cross = float(ma20_s.iloc[-2]) <= float(ma50_s.iloc[-2]) and ma20 > ma50
        ma_death_cross = float(ma20_s.iloc[-2]) >= float(ma50_s.iloc[-2]) and ma20 < ma50

        # 3. MACD (12, 26, 9)
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        hist = macd_line - signal_line
        hist_now = float(hist.iloc[-1])
        hist_prev = float(hist.iloc[-2])
        macd_cross_up = float(macd_line.iloc[-2]) <= float(signal_line.iloc[-2]) and float(macd_line.iloc[-1]) > float(signal_line.iloc[-1])
        macd_cross_dn = float(macd_line.iloc[-2]) >= float(signal_line.iloc[-2]) and float(macd_line.iloc[-1]) < float(signal_line.iloc[-1])

        # 4. Stochastic RSI (14, 3, 3)
        rsi_min = rsi_series.rolling(14).min()
        rsi_max = rsi_series.rolling(14).max()
        stoch_rsi = ((rsi_series - rsi_min) / ((rsi_max - rsi_min) + 1e-9)) * 100.0
        stoch_k = float(stoch_rsi.rolling(3).mean().iloc[-1])
        stoch_d = float(stoch_rsi.rolling(3).mean().rolling(3).mean().iloc[-1])

        # 5. Bollinger Bands (20, 2)
        bb_std = close.rolling(20).std()
        bb_upper = ma20_s + 2.0 * bb_std
        bb_lower = ma20_s - 2.0 * bb_std
        pct_b = float((curr_px - bb_lower.iloc[-1]) / max(float(bb_upper.iloc[-1] - bb_lower.iloc[-1]), 1e-9))
        bb_width_pct = float((bb_upper.iloc[-1] - bb_lower.iloc[-1]) / max(ma20, 1e-9) * 100.0)

        # 6. Ichimoku Cloud (9, 26, 52)
        tenkan = float((high.rolling(9).max().iloc[-1] + low.rolling(9).min().iloc[-1]) / 2.0)
        kijun = float((high.rolling(26).max().iloc[-1] + low.rolling(26).min().iloc[-1]) / 2.0)
        senkou_a = float(((high.rolling(9).max() + low.rolling(9).min()) / 4.0 + (high.rolling(26).max() + low.rolling(26).min()) / 4.0).iloc[-26] if len(df) > 60 else kijun)
        senkou_b = float(((high.rolling(52).max() + low.rolling(52).min()) / 2.0).iloc[-26] if len(df) > 80 else kijun)
        cloud_top = max(senkou_a, senkou_b)
        cloud_bot = min(senkou_a, senkou_b)

        # 7. RSI Divergence check
        p_now_low = float(low.iloc[-6:].min())
        p_old_low = float(low.iloc[-24:-8].min())
        r_now_low = float(rsi_series.iloc[-6:].min())
        r_old_low = float(rsi_series.iloc[-24:-8].min())
        bull_div = p_now_low < p_old_low * 0.999 and r_now_low > r_old_low + 3.0

        p_now_high = float(high.iloc[-6:].max())
        p_old_high = float(high.iloc[-24:-8].max())
        r_now_high = float(rsi_series.iloc[-6:].max())
        r_old_high = float(rsi_series.iloc[-24:-8].max())
        bear_div = p_now_high > p_old_high * 1.001 and r_now_high < r_old_high - 3.0

        score = 0.0
        bullets: List[str] = []

        # RSI multi-timeframe scoring
        if rsi < 30 and rsi > rsi_prev:
            score += 20.0
        elif 52 <= rsi <= 68:
            score += 14.0
        elif rsi > 76:
            score -= 15.0
        elif 32 <= rsi <= 48:
            score -= 14.0
        elif rsi <= 30:
            score += 8.0

        if rsi_ltf > 53 and rsi_htf > 52:
            score += 10.0
        elif rsi_ltf < 47 and rsi_htf < 48:
            score -= 10.0

        bullets.append(
            f"وضعیت RSI-14 در سه تایم‌فریم: ۱۵دقیقه={rsi_ltf:.1f} | ۱ساعته={rsi:.1f} | ۴ساعته={rsi_htf:.1f}."
        )

        # Moving Averages (MA20 / MA50 / MA200) scoring
        above_cnt = sum([curr_px > ma20, curr_px > ma50, curr_px > ma200])
        if ma_golden_cross:
            score += 22.0
            ma_status_fa = "تقاطع طلایی صعودی MA20 به بالای MA50 🚀"
        elif ma_death_cross:
            score -= 22.0
            ma_status_fa = "تقاطع مرگ نزولی MA20 به زیر MA50 📉"
        elif above_cnt == 3 and ma20 > ma50:
            score += 18.0
            ma_status_fa = "قیمت بالای هر سه میانگین MA20، MA50 و MA200"
        elif above_cnt == 0 and ma20 < ma50:
            score -= 18.0
            ma_status_fa = "قیمت زیر هر سه میانگین MA20، MA50 و MA200"
        elif curr_px > ma50:
            score += 8.0
            ma_status_fa = "بالای MA50 و در حال تثبیت"
        else:
            score -= 8.0
            ma_status_fa = "زیر MA50 (فشار میانگین متحرک)"

        bullets.append(
            f"میانگین‌های متحرک (MA): MA20=${self.fmt_px(ma20)} | MA50=${self.fmt_px(ma50)} | MA200=${self.fmt_px(ma200)} ({ma_status_fa})."
        )

        # MACD logic
        if macd_cross_up:
            score += 20.0
            macd_status_fa = "تقاطع صعودی تازه (Bullish Cross)"
        elif macd_cross_dn:
            score -= 20.0
            macd_status_fa = "تقاطع نزولی تازه (Bearish Cross)"
        elif hist_now > 0 and hist_now > hist_prev:
            score += 14.0
            macd_status_fa = "مثبت و شتاب‌گیرنده صعودی"
        elif hist_now < 0 and hist_now < hist_prev:
            score -= 14.0
            macd_status_fa = "منفی و در حال تقویت ریزش"
        elif hist_now > 0:
            macd_status_fa = "مثبت (در حال کاهش شتاب)"
        else:
            macd_status_fa = "منفی (در حال کاهش فشار فروش)"

        bullets.append(f"وضعیت مکدی (MACD): {macd_status_fa} | StochRSI: {stoch_k:.0f}/{stoch_d:.0f} | بولینگر %B: {pct_b*100:.0f}%.")

        # Ichimoku
        if curr_px > cloud_top and tenkan >= kijun:
            score += 14.0
        elif curr_px < cloud_bot and tenkan <= kijun:
            score -= 14.0

        # Divergences
        if bull_div:
            score += 24.0
            bullets.insert(0, "واگرایی مثبت (Bullish RSI Divergence) بین کف‌های قیمت و RSI شناسایی شد!")
        elif bear_div:
            score -= 24.0
            bullets.insert(0, "واگرایی منفی (Bearish RSI Divergence) در سقف قیمت شناسایی شد!")

        score = round(self.clamp(score), 1)
        direction = self.score_to_direction(score, threshold=15.0)
        confidence = round(min(95.0, 50.0 + abs(score) * 0.45), 1)

        summary = (
            f"RSI(15m/1h/4h)=[{rsi_ltf:.0f}/{rsi:.0f}/{rsi_htf:.0f}] | {ma_status_fa} | مکدی: {macd_status_fa} (امتیاز {score:+.0f})."
        )

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
                "rsi_15m": round(rsi_ltf, 1),
                "rsi_14": round(rsi, 1),
                "rsi_4h": round(rsi_htf, 1),
                "ma20": round(ma20, 6),
                "ma50": round(ma50, 6),
                "ma200": round(ma200, 6),
                "ema21": round(ema21, 6),
                "ema50": round(ema50, 6),
                "ema200": round(ema200, 6),
                "ma_status_fa": ma_status_fa,
                "macd_status_fa": macd_status_fa,
                "macd_hist": round(hist_now, 6),
                "stoch_k": round(stoch_k, 1),
                "stoch_d": round(stoch_d, 1),
                "bollinger_pct_b": round(pct_b, 3),
                "bollinger_width_pct": round(bb_width_pct, 2),
                "ichimoku_above_cloud": curr_px > cloud_top,
                "bullish_divergence": bull_div,
                "bearish_divergence": bear_div,
            },
        )
