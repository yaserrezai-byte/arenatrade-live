from typing import Any, Dict, List, Optional

from agents.base_agent import BaseAgent
from core.database import db
from core.models import AgentReport


class HistoricalComparisonAgent(BaseAgent):
    """
    Stateful Structural Memory & Historical Comparison Agent:
    Compares the current market state (S/R Zones, Diagonal Trendlines, Price, OI, Agent Scores)
    against previously saved analyses in SQLite for the exact same (symbol, timeframe).
    Detects S/R Flips (Resistance -> Support, Support -> Resistance), Trendline Breakouts,
    Support/Resistance Zone Defenses, and Previous Signal Outcomes.
    """

    agent_id = "historical_comparison"
    agent_name_fa = "ایجنت مقایسه زمانی با تحلیل قبلی (S/R و خط روند)"
    category_fa = "رهگیری شکست/تبدیل سطوح، تغییرات خط روند و شتاب جریان پول"

    async def analyze(self, symbol: str, context: Dict[str, Any]) -> AgentReport:
        coin = symbol.upper().replace("USDT", "").strip()
        tf = str(context.get("timeframe", "1h"))
        curr_sr: Dict[str, Any] = context.get("current_sr_metrics", {})
        curr_pa: Dict[str, Any] = context.get("current_pa_metrics", {})
        curr_mf: Dict[str, Any] = context.get("current_mf_metrics", {})
        curr_ind: Dict[str, Any] = context.get("current_ind_metrics", {})
        pre_composite: float = float(context.get("pre_composite_score", 0.0))

        curr_px = float(curr_pa.get("current_price", 0.0))
        atr = float(curr_pa.get("atr", max(curr_px * 0.01, 1e-6)))
        curr_s1 = float(curr_sr.get("s1", curr_px - atr))
        curr_r1 = float(curr_sr.get("r1", curr_px + atr))
        curr_oi = float(curr_mf.get("total_open_interest_usd", 0.0))

        history = db.get_previous_signals_for_symbol(coin, timeframe=tf, limit=5)
        if not history:
            # First-ever scan for this (symbol, timeframe): establish baseline memory!
            return AgentReport(
                agent_id=self.agent_id,
                agent_name_fa=self.agent_name_fa,
                category_fa=self.category_fa,
                symbol=coin,
                score=0.0,
                direction="NEUTRAL",
                confidence=55.0,
                summary_fa=f"ثبت نقطه مرجع اولیه (Baseline) برای {coin} در تایم‌فریم {tf}؛ از تحلیل بعدی تمام تغییرات S/R و خطوط روند با این وضعیت مقایسه می‌شوند.",
                bullets_fa=[
                    f"محدوده حمایت مرجع ثبت‌شده: ${self.fmt_px(curr_s1)} | محدوده مقاومت مرجع: ${self.fmt_px(curr_r1)}.",
                    "از اسکن بعدی، شکست سطوح قبلی، تبدیل مقاومت به حمایت (S/R Flip) و تغییرات خط روند سنجیده خواهد شد.",
                ],
                metrics={
                    "has_previous": False,
                    "baseline_created": True,
                },
            )

        # Pick the most meaningful previous snapshot (prefer one with slight time separation if multiple exist within seconds, else latest)
        prev = history[0]
        for cand in history:
            if abs(float(cand.get("current_price", curr_px)) - curr_px) > 0.0005 * curr_px:
                prev = cand
                break

        prev_px = float(prev.get("current_price", curr_px))
        prev_score = float(prev.get("composite_score", 0.0))
        prev_dir = str(prev.get("direction", "NEUTRAL"))
        prev_ts = str(prev.get("timestamp_iso", ""))

        # Extract previous S/R & MoneyFlow metrics from previous agent_reports
        prev_sr_rep: Optional[Dict[str, Any]] = None
        prev_mf_rep: Optional[Dict[str, Any]] = None
        prev_ind_rep: Optional[Dict[str, Any]] = None
        for r in prev.get("agent_reports", []):
            if r.get("agent_id") == "support_resistance":
                prev_sr_rep = r.get("metrics", {})
            elif r.get("agent_id") == "money_flow":
                prev_mf_rep = r.get("metrics", {})
            elif r.get("agent_id") == "indicators":
                prev_ind_rep = r.get("metrics", {})

        prev_s1 = float((prev_sr_rep or {}).get("s1", prev_px - atr))
        prev_r1 = float((prev_sr_rep or {}).get("r1", prev_px + atr))
        prev_s1_zone = (prev_sr_rep or {}).get("s1_zone") or {"low": prev_s1 - 0.2 * atr, "high": prev_s1 + 0.2 * atr}
        prev_r1_zone = (prev_sr_rep or {}).get("r1_zone") or {"low": prev_r1 - 0.2 * atr, "high": prev_r1 + 0.2 * atr}
        prev_desc_tl = (prev_sr_rep or {}).get("descending_tl")
        prev_asc_tl = (prev_sr_rep or {}).get("ascending_tl")
        prev_oi = float((prev_mf_rep or {}).get("total_open_interest_usd", 0.0))

        px_chg_pct = ((curr_px - prev_px) / max(prev_px, 1e-9)) * 100.0
        score_delta = round(pre_composite - prev_score, 1)
        oi_chg_pct = (((curr_oi - prev_oi) / prev_oi) * 100.0) if prev_oi > 0 else 0.0

        score = 0.0
        bullets: List[str] = []
        sr_transition_fa = "نوسان بین حمایت و مقاومت تحلیل قبلی"

        # 1. S/R Zone Breakout & Flip Comparison vs Previous Analysis!
        if prev_px <= prev_r1_zone["high"] and curr_px > prev_r1_zone["high"] + 0.10 * atr:
            score += 34.0
            sr_transition_fa = f"شکست مقاومت قبلی (${self.fmt_px(prev_r1)}) و تبدیل به حمایت (Bullish S/R Flip) 🚀"
            bullets.append(
                f"شکست صعودی محدوده مقاومت تحلیل قبلی (${self.fmt_px(prev_r1_zone['low'])}–${self.fmt_px(prev_r1_zone['high'])})؛ این سطح اکنون نقش حمایت جدید (S/R Flip) دارد!"
            )
        elif prev_px >= prev_s1_zone["low"] and curr_px < prev_s1_zone["low"] - 0.10 * atr:
            score -= 34.0
            sr_transition_fa = f"از دست رفتن حمایت قبلی (${self.fmt_px(prev_s1)}) و تبدیل به مقاومت (Bearish S/R Flip) 📉"
            bullets.append(
                f"شکست نزولی محدوده حمایت تحلیل قبلی (${self.fmt_px(prev_s1_zone['low'])}–${self.fmt_px(prev_s1_zone['high'])})؛ این ناحیه اکنون به سد مقاومتی تبدیل شده است!"
            )
        else:
            # Check structural shift of S1 and R1 levels themselves
            s1_shift_pct = ((curr_s1 - prev_s1) / max(prev_s1, 1e-9)) * 100.0
            r1_shift_pct = ((curr_r1 - prev_r1) / max(prev_r1, 1e-9)) * 100.0
            if s1_shift_pct > 0.35:
                score += 18.0
                sr_transition_fa = f"بالا آمدن کف حمایتی از ${self.fmt_px(prev_s1)} به ${self.fmt_px(curr_s1)} (ساختار صعودی)"
                bullets.append(
                    f"مقایسه سطوح S/R: محدوده حمایت اول از ${self.fmt_px(prev_s1)} به ${self.fmt_px(curr_s1)} ارتقا یافته است (تشکیل کف بالاتر)."
                )
            elif r1_shift_pct < -0.35:
                score -= 18.0
                sr_transition_fa = f"پایین آمدن سقف مقاومتی از ${self.fmt_px(prev_r1)} به ${self.fmt_px(curr_r1)} (فشار عرضه)"
                bullets.append(
                    f"مقایسه سطوح S/R: سد مقاومت اول از ${self.fmt_px(prev_r1)} به ${self.fmt_px(curr_r1)} پایین‌تر آمده است (تداوم سقف‌های پایین‌تر)."
                )
            else:
                sr_transition_fa = f"حفظ کانال نوسانی قبلی [${self.fmt_px(prev_s1)} – ${self.fmt_px(prev_r1)}]"
                bullets.append(
                    f"محدوده‌های حمایت (${self.fmt_px(curr_s1)}) و مقاومت (${self.fmt_px(curr_r1)}) نسبت به تحلیل قبلی بدون شکست ماژور پایدار مانده‌اند."
                )

        # 2. Trendline State Transition vs Previous Analysis
        curr_desc_tl = curr_sr.get("descending_tl")
        curr_asc_tl = curr_sr.get("ascending_tl")
        tl_transition_fa = "بدون تغییر فاز در خطوط روند"

        if curr_desc_tl:
            c_st = curr_desc_tl["status"]
            p_st = prev_desc_tl.get("status") if isinstance(prev_desc_tl, dict) else None
            if c_st == "BREAKOUT_UP" and p_st != "BREAKOUT_UP":
                score += 24.0
                tl_transition_fa = "شکست تازه خط روند نزولی نسبت به تحلیل قبل!"
                bullets.append(f"تغییر فاز خط روند: خط روند نزولی که در تحلیل قبل مقاومت بود، اکنون در ${self.fmt_px(curr_desc_tl['current_price_on_line'])} به بالا شکسته شده است!")
            elif c_st == "RETEST_SUPPORT":
                score += 14.0
                tl_transition_fa = "در حال پولبک به خط روند نزولی شکسته‌شده"

        if curr_asc_tl:
            c_st = curr_asc_tl["status"]
            p_st = prev_asc_tl.get("status") if isinstance(prev_asc_tl, dict) else None
            if c_st == "BREAKDOWN_DOWN" and p_st != "BREAKDOWN_DOWN":
                score -= 24.0
                tl_transition_fa = "شکست تازه خط روند صعودی رو به پایین نسبت به تحلیل قبل!"
                bullets.append(f"هشدار تغییر فاز خط روند: خط روند صعودی معتبر قبلی اکنون در ${self.fmt_px(curr_asc_tl['current_price_on_line'])} به پایین شکسته شد!")
            elif c_st == "BOUNCE_SUPPORT":
                score += 14.0
                tl_transition_fa = "حفظ و واکنش مجدد به خط روند صعودی"

        # 3. Momentum, Money Flow & RSI/MA Delta vs Previous Scan
        curr_rsi = float(curr_ind.get("rsi_14", 50.0))
        prev_rsi = float((prev_ind_rep or {}).get("rsi_14", curr_rsi))
        rsi_delta = round(curr_rsi - prev_rsi, 1)

        curr_flow_1h = float(curr_mf.get("net_inflow_1h_usd", 0.0))
        prev_flow_1h = float((prev_mf_rep or {}).get("net_inflow_1h_usd", curr_flow_1h))
        flow_flip_fa = ""
        if prev_flow_1h < 0 and curr_flow_1h > 0:
            score += 12.0
            flow_flip_fa = "چرخش از خروج پول به ورود خالص پول 🟢"
        elif prev_flow_1h > 0 and curr_flow_1h < 0:
            score -= 12.0
            flow_flip_fa = "چرخش از ورود پول به خروج پول 🔴"

        if abs(score_delta) >= 8.0:
            score += float(self.clamp(score_delta * 0.55, -20.0, 20.0))
            bullets.append(
                f"تغییرات نسبت به اسکن قبلی ({prev_ts[-12:-4] or prev_ts}): قیمت {px_chg_pct:+.2f}% | RSI از {prev_rsi:.1f} به {curr_rsi:.1f} | امتیاز کل {prev_score:+.1f} ⬅️ {pre_composite:+.1f}."
            )
        else:
            bullets.append(
                f"نسبت به اسکن قبلی ({prev_ts[-12:-4] or prev_ts}): قیمت {px_chg_pct:+.2f}% | RSI: {prev_rsi:.1f}⬅️{curr_rsi:.1f} | تغییر OI: {oi_chg_pct:+.2f}% {('| ' + flow_flip_fa) if flow_flip_fa else ''}."
            )

        # 4. Evaluate Previous Signal Status (TP / SL / Floating PnL)
        prev_sig_status_fa = "تحلیل قبلی خنثی (NEUTRAL) بود"
        if prev_dir in ("LONG", "SHORT"):
            p_sl = float(prev.get("stop_loss", 0.0))
            p_tp1 = float(prev.get("tp1", 0.0))
            p_tp2 = float(prev.get("tp2", 0.0))
            if prev_dir == "LONG":
                if curr_px >= p_tp2:
                    prev_sig_status_fa = f"سیگنال LONG قبلی به تارگت دوم (TP2: ${self.fmt_px(p_tp2)}) رسید ✅"
                elif curr_px >= p_tp1:
                    prev_sig_status_fa = f"سیگنال LONG قبلی تارگت اول (TP1: ${self.fmt_px(p_tp1)}) را تاچ کرد ✅"
                elif curr_px <= p_sl:
                    prev_sig_status_fa = f"سیگنال LONG قبلی با برخورد به حد ضرر (${self.fmt_px(p_sl)}) باطل شد ❌"
                else:
                    prev_sig_status_fa = f"سیگنال LONG قبلی همچنان فعال است ({px_chg_pct:+.2f}% نسبت به ورود قبلی)"
            else:
                if curr_px <= p_tp2:
                    prev_sig_status_fa = f"سیگنال SHORT قبلی به تارگت دوم (TP2: ${self.fmt_px(p_tp2)}) رسید ✅"
                elif curr_px <= p_tp1:
                    prev_sig_status_fa = f"سیگنال SHORT قبلی تارگت اول (TP1: ${self.fmt_px(p_tp1)}) را تاچ کرد ✅"
                elif curr_px >= p_sl:
                    prev_sig_status_fa = f"سیگنال SHORT قبلی با عبور از حد ضرر (${self.fmt_px(p_sl)}) باطل شد ❌"
                else:
                    prev_sig_status_fa = f"سیگنال SHORT قبلی همچنان فعال است ({-px_chg_pct:+.2f}% سود/ضرر بدون اهرم)"
            bullets.append(f"وضعیت سیگنال قبلی: {prev_sig_status_fa}.")

        score = round(self.clamp(score), 1)
        direction = self.score_to_direction(score, threshold=14.0)
        confidence = round(min(94.0, 52.0 + abs(score) * 0.45), 1)

        summary = f"{sr_transition_fa} | Δامتیاز: {score_delta:+.1f} | {prev_sig_status_fa}"

        return AgentReport(
            agent_id=self.agent_id,
            agent_name_fa=self.agent_name_fa,
            category_fa=self.category_fa,
            symbol=coin,
            score=score,
            direction=direction,
            confidence=confidence,
            summary_fa=summary,
            bullets_fa=bullets[:4],
            metrics={
                "has_previous": True,
                "previous_signal_id": prev.get("signal_id"),
                "previous_timestamp": prev_ts,
                "previous_direction": prev_dir,
                "previous_price": prev_px,
                "price_change_pct": round(px_chg_pct, 3),
                "previous_score": prev_score,
                "score_delta": score_delta,
                "oi_change_pct": round(oi_chg_pct, 2),
                "prev_s1": round(prev_s1, 6),
                "prev_r1": round(prev_r1, 6),
                "prev_rsi": prev_rsi,
                "curr_rsi": curr_rsi,
                "rsi_delta": rsi_delta,
                "flow_flip_fa": flow_flip_fa,
                "sr_transition_fa": sr_transition_fa,
                "tl_transition_fa": tl_transition_fa,
                "prev_signal_status_fa": prev_sig_status_fa,
            },
        )
