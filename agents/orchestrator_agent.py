import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from agents.fundamental_agent import FundamentalAgent
from agents.indicator_agent import TechnicalIndicatorAgent
from agents.memory_comparison_agent import HistoricalComparisonAgent
from agents.money_flow_agent import MoneyFlowAgent
from agents.news_agent import NewsSentimentAgent
from agents.price_action_agent import PriceActionAgent
from agents.sr_agent import SupportResistanceAgent
from agents.telegram_agent import telegram_agent
from agents.trend_agent import TrendDetectionAgent
from core.config import settings
from core.database import db
from core.llm_router import llm_router
from core.models import AgentReport, FuturesSignal, SignalDirection
from data_sources.market_data import market_hub
from data_sources.news_fundamental import news_fundamental_hub


class OrchestratorAgent:
    """
    Pure Market Analysis Strategist & Futures Risk Engine (100% Separated from Copy-Trading):
    - Executes all 8 pure analytical agents concurrently (Price Action, Money Flow, Indicators,
      Trend, S/R Zones & Trendlines, Fundamentals, News, and 15m Historical Comparison).
    - Emits standalone 'MARKET_ANALYSIS' Futures Signals to Telegram independently of Copy-Trading.
    """

    AGENT_WEIGHTS = {
        "support_resistance": 0.20,
        "trend": 0.18,
        "money_flow": 0.16,
        "price_action": 0.14,
        "historical_comparison": 0.12,
        "indicators": 0.11,
        "news": 0.05,
        "fundamental": 0.04,
    }

    def __init__(self) -> None:
        self.price_agent = PriceActionAgent()
        self.money_agent = MoneyFlowAgent()
        self.indicator_agent = TechnicalIndicatorAgent()
        self.trend_agent = TrendDetectionAgent()
        self.sr_agent = SupportResistanceAgent()
        self.fund_agent = FundamentalAgent()
        self.news_agent = NewsSentimentAgent()
        self.memory_agent = HistoricalComparisonAgent()

    async def analyze_symbol(
        self,
        symbol: str,
        timeframe: str | None = None,
        send_to_telegram: bool | None = None,
    ) -> Dict[str, Any]:
        coin = symbol.upper().replace("USDT", "").strip()
        tf_primary = timeframe or str(settings.get("PRIMARY_TIMEFRAME", "1h"))
        tf_htf = str(settings.get("HTF_TIMEFRAME", "4h"))
        tf_ltf = str(settings.get("LTF_TIMEFRAME", "15m"))

        # 1. Gather all market, derivatives, news, and fundamental datasets concurrently
        df_primary, df_htf, df_ltf, derivatives, news_items, fundamentals = await asyncio.gather(
            market_hub.fetch_ohlcv(coin, tf_primary, 210),
            market_hub.fetch_ohlcv(coin, tf_htf, 160),
            market_hub.fetch_ohlcv(coin, tf_ltf, 140),
            market_hub.fetch_derivatives_bundle(coin),
            news_fundamental_hub.fetch_news(coin),
            news_fundamental_hub.fetch_fundamentals(coin),
        )

        context: Dict[str, Any] = {
            "symbol": coin,
            "timeframe": tf_primary,
            "df_primary": df_primary,
            "df_htf": df_htf,
            "df_ltf": df_ltf,
            "derivatives": derivatives,
            "news": news_items,
            "fundamentals": fundamentals,
        }

        # 2. Run all 7 primary pure-analysis agents concurrently (Zero Copy-Trader mixing)
        reports: List[AgentReport] = list(
            await asyncio.gather(
                self.price_agent.analyze(coin, context),
                self.money_agent.analyze(coin, context),
                self.indicator_agent.analyze(coin, context),
                self.trend_agent.analyze(coin, context),
                self.sr_agent.analyze(coin, context),
                self.fund_agent.analyze(coin, context),
                self.news_agent.analyze(coin, context),
            )
        )

        report_map = {r.agent_id: r for r in reports}

        # Pre-composite score before historical delta comparison
        pre_composite = sum(r.score * self.AGENT_WEIGHTS.get(r.agent_id, 0.1) for r in reports) / 0.88
        context["current_sr_metrics"] = report_map["support_resistance"].metrics
        context["current_pa_metrics"] = report_map["price_action"].metrics
        context["current_mf_metrics"] = report_map["money_flow"].metrics
        context["current_ind_metrics"] = report_map["indicators"].metrics
        context["pre_composite_score"] = round(pre_composite, 1)

        # 2b. Run Agent #8: Stateful Historical Comparison Agent (S/R Flip & Trendline Transitions vs Previous Scan)
        mem_report = await self.memory_agent.analyze(coin, context)
        reports.append(mem_report)
        report_map[mem_report.agent_id] = mem_report

        # 3. Weighted Confluence Calculation
        composite_score = 0.0
        for rep in reports:
            w = self.AGENT_WEIGHTS.get(rep.agent_id, 0.1)
            composite_score += rep.score * w

        # Agreement synergy bonus: how many agents agree on the dominant side?
        long_votes = sum(1 for r in reports if r.direction == "LONG")
        short_votes = sum(1 for r in reports if r.direction == "SHORT")

        if long_votes >= 5 and composite_score > 0:
            composite_score = min(98.0, composite_score * 1.18)
        elif short_votes >= 5 and composite_score < 0:
            composite_score = max(-98.0, composite_score * 1.18)

        composite_score = round(composite_score, 1)

        # Determine raw direction
        if composite_score >= 16.0:
            direction: SignalDirection = "LONG"
        elif composite_score <= -16.0:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"

        # 4. Compute Institutional Futures Entry, Stop-Loss, TP1/TP2/TP3, Leverage
        closed_candle_px = float(df_primary["close"].iloc[-1])
        live_mark_px = float(derivatives.get("mark_price") or 0.0)
        # Indicators use only confirmed candles, while entries/stops are anchored to the
        # current executable mark price. Reject implausible/stale cross-source marks.
        if live_mark_px > 0 and abs(live_mark_px - closed_candle_px) / closed_candle_px <= 0.12:
            curr_px = live_mark_px
        else:
            curr_px = closed_candle_px
        atr = float(report_map["price_action"].metrics.get("atr", curr_px * 0.012))
        atr_pct = float(report_map["price_action"].metrics.get("atr_pct", 1.2))
        s1 = float(report_map["support_resistance"].metrics.get("s1", curr_px - 1.5 * atr))
        s2 = float(report_map["support_resistance"].metrics.get("s2", curr_px - 3.0 * atr))
        r1 = float(report_map["support_resistance"].metrics.get("r1", curr_px + 1.5 * atr))
        r2 = float(report_map["support_resistance"].metrics.get("r2", curr_px + 3.0 * atr))

        min_rr = float(settings.get("MIN_RISK_REWARD_RATIO", 2.0))
        cost_pct = float(settings.get("ESTIMATED_ROUND_TRIP_COST_PCT", 0.18))
        fib_m = report_map["support_resistance"].metrics.get("fibonacci") or {}
        fib_ext_1272 = float(fib_m.get("extension_1.272") or 0.0)
        fib_ext_1618 = float(fib_m.get("extension_1.618") or 0.0)
        structural_tp2_available = False
        target_basis = {"tp1": "ATR_FALLBACK", "tp2": "ATR_FALLBACK", "tp3": "ATR_FALLBACK"}

        if direction == "LONG" or (direction == "NEUTRAL" and composite_score >= 0):
            entry_low = curr_px - 0.22 * atr
            entry_high = curr_px + 0.08 * atr
            # Worst-case fill for LONG is the upper edge of the entry range.
            conservative_entry = entry_high
            structural_sl = s1 - 0.35 * atr
            atr_sl = curr_px - 1.75 * atr
            stop_loss = structural_sl if (0.6 * atr <= (curr_px - structural_sl) <= 2.8 * atr) else atr_sl
            risk_dist = max(conservative_entry - stop_loss, 0.5 * atr)
            cost_abs = conservative_entry * cost_pct / 100.0
            effective_risk = risk_dist + cost_abs

            candidates = sorted(
                {
                    round(px, 10): label
                    for px, label in (
                        (r1, "R1"), (r2, "R2"),
                        (fib_ext_1272, "FIB_EXT_1.272"),
                        (fib_ext_1618, "FIB_EXT_1.618"),
                    )
                    if px > conservative_entry + 0.15 * atr
                }.items()
            )

            def long_net_rr(px: float) -> float:
                return max(0.0, (px - conservative_entry - cost_abs) / max(effective_risk, 1e-9))

            tp1_item = next(((px, label) for px, label in candidates if long_net_rr(px) >= 0.8), None)
            tp2_item = next(((px, label) for px, label in candidates if long_net_rr(px) >= min_rr), None)
            if tp1_item and (not tp2_item or tp1_item[0] < tp2_item[0]):
                tp1, target_basis["tp1"] = tp1_item
            else:
                tp1 = conservative_entry + 0.9 * effective_risk + cost_abs
            if tp2_item:
                tp2, target_basis["tp2"] = tp2_item
                structural_tp2_available = True
            else:
                tp2 = conservative_entry + min_rr * effective_risk + cost_abs
            tp3_item = next(((px, label) for px, label in candidates if px > tp2 + 0.15 * atr), None)
            if tp3_item:
                tp3, target_basis["tp3"] = tp3_item
            else:
                tp3 = tp2 + max(effective_risk, 0.8 * atr)
            invalidation_fa = f"تثبیت کندل {tf_primary} زیر سطح حمایتی و حد ضرر ${self.price_agent.fmt_px(stop_loss)} سناریوی صعودی را باطل می‌کند."
            rr_tp2 = round(long_net_rr(tp2), 2)
        else:
            entry_low = curr_px - 0.08 * atr
            entry_high = curr_px + 0.22 * atr
            # Worst-case fill for SHORT is the lower edge of the entry range.
            conservative_entry = entry_low
            structural_sl = r1 + 0.35 * atr
            atr_sl = curr_px + 1.75 * atr
            stop_loss = structural_sl if (0.6 * atr <= (structural_sl - curr_px) <= 2.8 * atr) else atr_sl
            risk_dist = max(stop_loss - conservative_entry, 0.5 * atr)
            cost_abs = conservative_entry * cost_pct / 100.0
            effective_risk = risk_dist + cost_abs

            candidates = sorted(
                {
                    round(px, 10): label
                    for px, label in (
                        (s1, "S1"), (s2, "S2"),
                        (fib_ext_1272, "FIB_EXT_1.272"),
                        (fib_ext_1618, "FIB_EXT_1.618"),
                    )
                    if 0 < px < conservative_entry - 0.15 * atr
                }.items(),
                reverse=True,
            )

            def short_net_rr(px: float) -> float:
                return max(0.0, (conservative_entry - px - cost_abs) / max(effective_risk, 1e-9))

            tp1_item = next(((px, label) for px, label in candidates if short_net_rr(px) >= 0.8), None)
            tp2_item = next(((px, label) for px, label in candidates if short_net_rr(px) >= min_rr), None)
            if tp1_item and (not tp2_item or tp1_item[0] > tp2_item[0]):
                tp1, target_basis["tp1"] = tp1_item
            else:
                tp1 = conservative_entry - 0.9 * effective_risk - cost_abs
            if tp2_item:
                tp2, target_basis["tp2"] = tp2_item
                structural_tp2_available = True
            else:
                tp2 = conservative_entry - min_rr * effective_risk - cost_abs
            tp3_item = next(((px, label) for px, label in candidates if px < tp2 - 0.15 * atr), None)
            if tp3_item:
                tp3, target_basis["tp3"] = tp3_item
            else:
                tp3 = max(1e-9, tp2 - max(effective_risk, 0.8 * atr))
            invalidation_fa = f"تثبیت کندل {tf_primary} بالای مقاومت و حد ضرر ${self.price_agent.fmt_px(stop_loss)} سناریوی نزولی را باطل می‌کند."
            rr_tp2 = round(short_net_rr(tp2), 2)

        # Confidence Score (0..97)
        agreement_count = long_votes if direction == "LONG" else (short_votes if direction == "SHORT" else max(long_votes, short_votes))
        confidence = min(96.0, 42.0 + abs(composite_score) * 0.52 + agreement_count * 2.8)

        # Safety Vetoes (Black swan news, trading into S/R, or chasing a live price
        # that has moved too far from the last confirmed primary candle).
        veto_reasons = [r.veto_reason_fa for r in reports if r.veto and r.veto_reason_fa]
        if direction in ("LONG", "SHORT") and not structural_tp2_available:
            veto_reasons.append(
                f"فیلتر R:R واقعی: هیچ هدف ساختاری معتبر (S/R یا اکستنشن فیبوناچی) با نسبت خالص حداقل 1:{min_rr:.1f} پس از هزینه‌ها وجود ندارد."
            )
        live_deviation_atr = abs(curr_px - closed_candle_px) / max(atr, 1e-9)
        if live_mark_px > 0 and live_deviation_atr > 0.90:
            veto_reasons.append(
                f"قیمت زنده {live_deviation_atr:.2f} ATR از آخرین کندل تاییدشده فاصله گرفته؛ برای جلوگیری از تعقیب قیمت باید منتظر تثبیت/پولبک ماند."
            )

        # Mandatory confirmed 4H regime alignment. The 15m trigger is allowed to
        # choose timing, but it can no longer open against or without the 4H trend.
        trend_m = report_map["trend"].metrics
        htf_bullish = bool(trend_m.get("htf_bullish", False))
        htf_bearish = bool(trend_m.get("htf_bearish", False))
        htf_regime = "BULLISH" if htf_bullish else ("BEARISH" if htf_bearish else "NEUTRAL")
        if settings.get("REQUIRE_4H_TREND_ALIGNMENT", True):
            if direction == "LONG" and not htf_bullish:
                veto_reasons.append(
                    "فیلتر روند ۴ساعته: روند 4H صعودی تایید نشده است؛ سیگنال LONG فقط تحت‌نظر می‌ماند."
                )
            elif direction == "SHORT" and not htf_bearish:
                veto_reasons.append(
                    "فیلتر روند ۴ساعته: روند 4H نزولی تایید نشده است؛ سیگنال SHORT فقط تحت‌نظر می‌ماند."
                )
        room_up_atr = min(
            float(report_map["support_resistance"].metrics.get("room_up_atr", 2.0)),
            (r1 - curr_px) / max(atr, 1e-9),
        )
        room_dn_atr = min(
            float(report_map["support_resistance"].metrics.get("room_dn_atr", 2.0)),
            (curr_px - s1) / max(atr, 1e-9),
        )
        if direction == "LONG" and room_up_atr < 0.35:
            confidence -= 14.0
        elif direction == "SHORT" and room_dn_atr < 0.35:
            confidence -= 14.0

        confidence = round(max(15.0, min(97.0, confidence)), 1)

        # Dynamic Leverage Calculation based on Volatility (ATR%) & Confidence
        max_lev_cfg = int(settings.get("MAX_LEVERAGE", 10))
        sl_dist_pct = (abs(curr_px - stop_loss) / max(curr_px, 1e-9)) * 100.0
        # Target max ~15-20% margin drawdown at SL in isolated mode -> safe_lev ~= 15 / sl_dist_pct
        vol_safe_lev = int(max(2, min(max_lev_cfg, round(14.0 / max(sl_dist_pct, 0.6)))))
        if confidence < 65:
            vol_safe_lev = min(vol_safe_lev, 5)
        recommended_leverage = max(2, min(max_lev_cfg, vol_safe_lev))

        # Pure Market Analysis signal source (100% decoupled from Copy-Trading)
        copied_addr = None
        copied_label = None
        signal_source = "MARKET_ANALYSIS"

        min_conf = float(settings.get("MIN_CONFIDENCE_SCORE", 60.0))
        actionable = (
            direction in ("LONG", "SHORT")
            and confidence >= min_conf
            and rr_tp2 >= min_rr
            and not veto_reasons
        )

        # 5. Executive Synthesis (Hybrid LLM if configured, else Deterministic Quantitative Persian Synthesis)
        exec_summary_fa, engine_used = await self._build_executive_summary(
            coin=coin,
            direction=direction,
            confidence=confidence,
            composite_score=composite_score,
            reports=reports,
            rr_tp2=rr_tp2,
            actionable=actionable,
            veto_reasons=veto_reasons,
            copied_label=copied_label,
        )

        sr_m = report_map["support_resistance"].metrics
        ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        sig_obj = FuturesSignal(
            signal_id=f"SIG-{coin}-{int(datetime.now(timezone.utc).timestamp())}-{uuid.uuid4().hex[:4].upper()}",
            timestamp_iso=ts_iso,
            symbol=coin,
            pair=f"{coin}USDT.P",
            timeframe=tf_primary,
            direction=direction,
            actionable=actionable,
            signal_source=signal_source,
            confidence=confidence,
            composite_score=composite_score,
            current_price=curr_px,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            risk_reward_tp2=rr_tp2,
            recommended_leverage=recommended_leverage,
            margin_mode="ISOLATED",
            risk_pct=float(settings.get("DEFAULT_RISK_PER_TRADE_PCT", 1.5)),
            executive_summary_fa=exec_summary_fa,
            invalidation_note_fa=invalidation_fa,
            ai_engine_used=engine_used,
            copied_trader_address=copied_addr,
            copied_trader_label=copied_label,
            sr_zones_and_trendlines={
                "s1_zone": sr_m.get("s1_zone"),
                "s2_zone": sr_m.get("s2_zone"),
                "r1_zone": sr_m.get("r1_zone"),
                "r2_zone": sr_m.get("r2_zone"),
                "ascending_tl": sr_m.get("ascending_tl"),
                "descending_tl": sr_m.get("descending_tl"),
                "poc_price": sr_m.get("poc_price"),
                "fib_618": sr_m.get("fib_618"),
                "fibonacci": sr_m.get("fibonacci"),
            },
            previous_comparison={
                **mem_report.metrics,
                "confirmed_primary_close": closed_candle_px,
                "live_mark_price": curr_px,
                "live_deviation_atr": round(live_deviation_atr, 3),
                "htf_4h_regime": htf_regime,
                "htf_4h_alignment_required": bool(settings.get("REQUIRE_4H_TREND_ALIGNMENT", True)),
                "safety_veto_reasons": veto_reasons,
                "risk_reward_method": "WORST_ENTRY_NET_OF_ESTIMATED_COSTS_TO_STRUCTURAL_TARGET",
                "estimated_round_trip_cost_pct": cost_pct,
                "conservative_entry_price": round(conservative_entry, 8),
                "structural_tp2_available": structural_tp2_available,
                "target_basis": target_basis,
                "data_policy": "CONFIRMED_CANDLES_PLUS_LIVE_MARK_WITH_CHASE_AND_4H_ALIGNMENT_VETO",
            },
            agent_reports=reports,
            telegram_sent=False,
        )

        sig_dict = sig_obj.model_dump()
        db.save_signal(sig_dict)

        # Dispatch to Telegram if requested or if actionable & auto-send enabled
        should_send = send_to_telegram if send_to_telegram is not None else (
            actionable and bool(settings.get("TELEGRAM_AUTO_SEND", True))
        )
        telegram_result = None
        if should_send:
            telegram_result = await telegram_agent.send_signal(sig_dict)
            if telegram_result.get("ok"):
                sig_dict["telegram_sent"] = True

        # Attach recent OHLCV chart data for instant interactive visualization in Dashboard
        chart_candles = []
        for _, row in df_primary.tail(75).iterrows():
            chart_candles.append(
                {
                    "t": int(row["timestamp"]),
                    "o": float(row["open"]),
                    "h": float(row["high"]),
                    "l": float(row["low"]),
                    "c": float(row["close"]),
                    "v": float(row["quote_volume"]),
                }
            )

        return {
            "signal": sig_dict,
            "chart_candles": chart_candles,
            "derivatives": derivatives,
            "fundamentals": fundamentals,
            "news": news_items[:8],
            "telegram_result": telegram_result,
            "telegram_preview": telegram_agent.format_signal_message(sig_dict),
        }

    async def _build_executive_summary(
        self,
        *,
        coin: str,
        direction: str,
        confidence: float,
        composite_score: float,
        reports: List[AgentReport],
        rr_tp2: float,
        actionable: bool,
        veto_reasons: List[str],
        copied_label: str | None,
    ) -> tuple[str, str]:
        compact_agents = [
            {"agent": r.agent_name_fa, "dir": r.direction, "score": r.score, "summary": r.summary_fa}
            for r in reports
        ]
        llm_json, model_label = await llm_router.generate_json(
            system_prompt=(
                "تو سرتیم ارشد معاملات فیوچرز ارز دیجیتال هستی. بر اساس خروجی ۸ ایجنت تخصصی، یک جمع‌بندی "
                "حرفه‌ای، دقیق و روان به زبان فارسی (۲ تا ۳ جمله) بنویس و در قالب JSON با کلید summary_fa برگردان."
            ),
            user_prompt=json.dumps(
                {
                    "symbol": coin,
                    "direction": direction,
                    "confidence": confidence,
                    "composite_score": composite_score,
                    "risk_reward": rr_tp2,
                    "actionable": actionable,
                    "copied_trader": copied_label,
                    "agents": compact_agents,
                },
                ensure_ascii=False,
            ),
        )
        if isinstance(llm_json, dict) and llm_json.get("summary_fa"):
            return str(llm_json["summary_fa"]), model_label

        # Deterministic Quantitative Strategist Summary (instant, zero API cost)
        if veto_reasons:
            return f"ورود به معامله توسط فیلتر ایمنی متوقف شد: {' | '.join(veto_reasons)}", model_label

        aligned = [r.agent_name_fa.replace("ایجنت ", "") for r in reports if r.direction == direction]
        opposing = [r.agent_name_fa.replace("ایجنت ", "") for r in reports if r.direction not in (direction, "NEUTRAL")]

        if direction == "NEUTRAL":
            return (
                f"در حال حاضر واگرایی بین ایجنت‌ها روی {coin} وجود دارد (امتیاز کل {composite_score:+.1f}) و بازار فاقد برتری آماری کم‌ریسک است؛ "
                "پیشنهاد می‌شود تا شکست معتبر حمایت یا مقاومت کلیدی از باز کردن پوزیشن اهرمی خودداری شود."
            ), model_label

        dir_fa = "لانگ (LONG)" if direction == "LONG" else "شورت (SHORT)"
        copy_sentence = (
            f" همچنین تریدر برتر فعال ({copied_label}) در همین جهت پوزیشن باز دارد که اعتبار سیگنال را دوچندان می‌کند."
            if copied_label
            else ""
        )
        opp_sentence = (
            f" (توجه به عدم همسویی نسبی در: {'، '.join(opposing[:2])})." if opposing else " و هیچ واگرایی مخربی در سایر ایجنت‌ها دیده نمی‌شود."
        )
        status_sentence = (
            f"ستاپ معاملاتی با نسبت ریسک به ریوارد 1:{rr_tp2:.2f} تایید شده است."
            if actionable
            else f"با توجه به امتیاز اطمینان الگوریتمی {confidence:.0f}/100 (هنوز کالیبره‌نشده به احتمال برد)، عدم ورود یا انتظار برای پولبک به محدوده بهینه توصیه می‌شود."
        )

        text = (
            f"همگرایی {len(aligned)} ایجنت تخصصی ({'، '.join(aligned[:5])}) برتری پوزیشن {dir_fa} را روی {coin} تایید می‌کند{opp_sentence}"
            f"{copy_sentence} {status_sentence}"
        )
        return text, model_label


orchestrator_agent = OrchestratorAgent()
