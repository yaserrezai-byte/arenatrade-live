import json
from typing import Any, Dict, List

from agents.base_agent import BaseAgent
from core.llm_router import llm_router
from core.models import AgentReport
from data_sources.news_fundamental import news_fundamental_hub


class NewsSentimentAgent(BaseAgent):
    agent_id = "news"
    agent_name_fa = "ایجنت رصد اخبار و سنتیمنت هوشمند"
    category_fa = "تحلیل فیدهای خبری لحظه‌ای و ریسک رویدادها"

    BULLISH_LEXICON = {
        "surge": 14,
        "rally": 14,
        "bullish": 15,
        "breakout": 14,
        "inflow": 16,
        "approval": 18,
        "approved": 18,
        "etf inflow": 20,
        "partnership": 12,
        "adoption": 14,
        "accumulation": 15,
        "upgrade": 12,
        "all-time high": 16,
        "buy": 8,
        "treasury": 11,
        "rate cut": 16,
        "institutional": 10,
    }

    BEARISH_LEXICON = {
        "hack": -32,
        "hacked": -35,
        "exploit": -35,
        "delist": -32,
        "lawsuit": -20,
        "sec charges": -24,
        "crash": -22,
        "plunge": -20,
        "outflow": -16,
        "liquidation": -14,
        "bearish": -15,
        "ban": -22,
        "insolvency": -35,
        "bankrupt": -35,
        "dump": -18,
        "tariff": -14,
        "inflation": -10,
    }

    BLACK_SWAN_PHRASES = [
        "protocol hacked",
        "bridge hacked",
        "exploited for $",
        "binance delists",
        "halts withdrawals",
        "files for bankruptcy",
    ]

    async def analyze(self, symbol: str, context: Dict[str, Any]) -> AgentReport:
        articles = context.get("news")
        if articles is None:
            articles = await news_fundamental_hub.fetch_news(symbol)

        if not articles:
            return AgentReport(
                agent_id=self.agent_id,
                agent_name_fa=self.agent_name_fa,
                category_fa=self.category_fa,
                symbol=symbol,
                score=0.0,
                direction="NEUTRAL",
                confidence=45.0,
                summary_fa="خبر فوری یا رویداد غیرعادی تاثیرگذاری در ساعت‌های اخیر منتشر نشده است.",
                bullets_fa=["جریان خبری آرام است و تصمیم‌گیری بیشتر بر پایه تکنیکال و جریان پول استوار است."],
                metrics={"headline_count": 0},
            )

        # Tier 1: Quantitative Financial Sentiment Lexicon with recency & asset-relevance weighting
        raw_score = 0.0
        veto = False
        veto_reason = None
        top_headlines: List[str] = []

        for idx, art in enumerate(articles[:10]):
            text = f"{art.get('title', '')} {art.get('summary', '')}".lower()
            weight = (1.35 if art.get("relevance") == "ASSET_SPECIFIC" else 0.75) * max(0.45, 1.0 - idx * 0.07)

            item_score = 0.0
            for word, pts in self.BULLISH_LEXICON.items():
                if word in text:
                    item_score += pts
            for word, pts in self.BEARISH_LEXICON.items():
                if word in text:
                    item_score += pts

            title_low = str(art.get("title", "")).lower()
            if art.get("relevance") == "ASSET_SPECIFIC":
                for bw in self.BLACK_SWAN_PHRASES:
                    if bw in title_low:
                        veto = True
                        veto_reason = f"هشدار ریسک خبری بحرانی ({bw.upper()}) در تیتر مرتبط با {symbol}: {art.get('title')}"

            raw_score += item_score * weight
            if idx < 3:
                rel_tag = "اختصاصی" if art.get("relevance") == "ASSET_SPECIFIC" else "کلان"
                top_headlines.append(f"[{art['source']} - {rel_tag}] {art['title']}")

        score = self.clamp(raw_score, -90.0, 90.0)
        summary_fa = ""

        # Tier 2: Optional LLM Deep Semantic Enrichment (when user sets Gemini/OpenRouter/DeepSeek/OpenAI key)
        llm_res, model_used = await llm_router.generate_json(
            system_prompt=(
                "You are a senior crypto futures news & macro analyst. Analyze the headlines for the requested coin "
                "and output JSON with keys: score (-100 to 100), summary_fa (concise Persian summary), "
                "critical_risk (boolean)."
            ),
            user_prompt=json.dumps(
                {"symbol": symbol, "headlines": [a["title"] for a in articles[:8]]},
                ensure_ascii=False,
            ),
        )
        if isinstance(llm_res, dict) and "score" in llm_res:
            llm_score = self.clamp(float(llm_res.get("score", score)))
            score = round(score * 0.4 + llm_score * 0.6, 1)
            summary_fa = str(llm_res.get("summary_fa") or "")

        score = round(self.clamp(score), 1)
        direction = self.score_to_direction(score, threshold=14.0)
        confidence = round(min(90.0, 48.0 + abs(score) * 0.45), 1)

        if not summary_fa:
            if direction == "LONG":
                summary_fa = f"برآیند اخبار لحظه‌ای برای {symbol} مثبت و حامی رشد قیمت است ({len(articles)} خبر بررسی شد، امتیاز {score:+.0f})."
            elif direction == "SHORT":
                summary_fa = f"فضای خبری غلبه تیترهای منفی/ریسک‌گریز را نشان می‌دهد ({len(articles)} خبر، امتیاز {score:+.0f})."
            else:
                summary_fa = f"سنتیمنت اخبار لحظه‌ای متعادل و بدون شوک خبری خاص است ({len(articles)} خبر، امتیاز {score:+.0f})."

        return AgentReport(
            agent_id=self.agent_id,
            agent_name_fa=self.agent_name_fa,
            category_fa=self.category_fa,
            symbol=symbol,
            score=score,
            direction=direction,
            confidence=confidence,
            summary_fa=summary_fa,
            bullets_fa=top_headlines or ["خبر شاخصی یافت نشد."],
            metrics={
                "headline_count": len(articles),
                "asset_specific_count": sum(1 for a in articles if a.get("relevance") == "ASSET_SPECIFIC"),
                "engine": model_used,
            },
            veto=veto,
            veto_reason_fa=veto_reason,
        )
