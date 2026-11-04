from typing import Any, Dict, List

from agents.base_agent import BaseAgent
from core.models import AgentReport
from data_sources.news_fundamental import news_fundamental_hub


class FundamentalAgent(BaseAgent):
    agent_id = "fundamental"
    agent_name_fa = "ایجنت تحلیل فاندامنتال و کلان"
    category_fa = "ارزش بازار، توکنومیکس، دامیننس و شاخص ترس و طمع"

    async def analyze(self, symbol: str, context: Dict[str, Any]) -> AgentReport:
        fund = context.get("fundamentals") or await news_fundamental_hub.fetch_fundamentals(symbol)

        fng = int(fund.get("fear_greed_value", 50))
        fng_label = str(fund.get("fear_greed_label", "Neutral"))
        btc_dom = float(fund.get("btc_dominance", 55.0))
        mcap_chg_24h = float(fund.get("market_cap_change_24h_pct", 0.0))
        mcap = float(fund.get("coin_market_cap_usd", 0.0))
        fdv = float(fund.get("coin_fdv_usd", 0.0))
        vol_24h = float(fund.get("coin_volume_24h_usd", 0.0))
        chg_7d = float(fund.get("price_change_7d_pct", 0.0))
        rank = int(fund.get("market_cap_rank", 0))

        score = 0.0
        bullets: List[str] = []

        # 1. Macro Market Cap Trend (24h)
        if mcap_chg_24h > 1.2:
            score += 18.0
            bullets.append(f"ارزش کل بازار کریپتو در ۲۴ ساعت اخیر {mcap_chg_24h:+.2f}% رشد کرده و فضای کلان ریسک‌پذیر است.")
        elif mcap_chg_24h < -1.2:
            score -= 18.0
            bullets.append(f"ارزش کل بازار کریپتو در ۲۴ ساعت گذشته {mcap_chg_24h:+.2f}% افت داشته و جریان کلان محتاطانه است.")
        else:
            bullets.append(f"تغییرات ۲۴ ساعته کل مارکت‌کپ بازار: {mcap_chg_24h:+.2f}% | دامیننس بیت‌کوین: {btc_dom:.1f}%.")

        # 2. Fear & Greed Regime (Contrarian at extremes, trend-supportive in middle)
        if fng <= 22:
            score += 16.0
            bullets.append(f"شاخص ترس و طمع در ناحیه «ترس شدید» ({fng} - {fng_label})؛ از دید فاندامنتال ناحیه جذاب تجمیع پله‌ای.")
        elif fng >= 80:
            score -= 14.0
            bullets.append(f"شاخص ترس و طمع در ناحیه «طمع شدید» ({fng} - {fng_label})؛ افزایش ریسک اصلاح ناگهانی بازار.")
        elif 52 <= fng < 78:
            score += 10.0
            bullets.append(f"شاخص ترس و طمع ({fng} - {fng_label}) نشان‌دهنده سنتیمنت مثبت و پایدار بازار است.")
        else:
            score -= 4.0 if fng < 45 else 0.0
            bullets.append(f"شاخص ترس و طمع بازار روی عدد {fng} ({fng_label}) قرار دارد.")

        # 3. Liquidity Turnover (24h Volume / Market Cap) & FDV Dilution Risk
        if mcap > 0:
            turnover_pct = (vol_24h / mcap) * 100.0
            if turnover_pct > 8.0:
                score += 10.0 if chg_7d >= 0 else -8.0
                bullets.append(f"نقدشوندگی و گردش مالی بالا (نسبت حجم به مارکت‌کپ: {turnover_pct:.1f}%، رتبه بازار: #{rank}).")

            if fdv > 0:
                fdv_ratio = fdv / mcap
                if fdv_ratio > 3.5:
                    score -= 12.0
                    bullets.append(
                        f"هشدار تورم توکن: نسبت FDV به مارکت‌کپ برابر {fdv_ratio:.1f}x است (ریسک فشار فروش آنلاک توکن‌ها)."
                    )
                elif fdv_ratio <= 1.35:
                    score += 10.0
                    bullets.append(f"توکنومیکس سالم: بخش عمده توکن‌ها در گردش است (FDV/MCap = {fdv_ratio:.2f}x).")

        # 4. BTC Dominance impact on Altcoins
        if symbol.upper() != "BTC":
            if btc_dom > 58.5 and mcap_chg_24h < 0:
                score -= 10.0
                bullets.append(f"دامیننس بالای بیت‌کوین ({btc_dom:.1f}%) فشار مضاعفی روی آلت‌کوین‌ها در ریزش‌ها وارد می‌کند.")

        score = round(self.clamp(score), 1)
        direction = self.score_to_direction(score, threshold=14.0)
        confidence = round(min(88.0, 48.0 + abs(score) * 0.45), 1)

        summary = f"ارزیابی فاندامنتال و کلان برای {symbol}: ترس و طمع {fng}، دامیننس BTC {btc_dom:.1f}% (امتیاز {score:+.0f})."

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
                "fear_greed": fng,
                "fear_greed_label": fng_label,
                "btc_dominance": btc_dom,
                "market_cap_rank": rank,
                "market_cap_usd": mcap,
                "fdv_usd": fdv,
                "price_change_7d_pct": round(chg_7d, 2),
            },
        )
