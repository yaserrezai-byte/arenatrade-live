from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from agents.base_agent import BaseAgent
from core.models import AgentReport


class SupportResistanceAgent(BaseAgent):
    agent_id = "support_resistance"
    agent_name_fa = "ایجنت محدوده‌های حمایت/مقاومت و خطوط روند"
    category_fa = "نواحی S/R، خطوط روند داینامیک، فیبوناچی و POC"

    @staticmethod
    def _cluster_zones(
        levels: List[float],
        df: pd.DataFrame,
        atr: float,
        tolerance_pct: float = 0.0055,
    ) -> List[Dict[str, Any]]:
        if not levels:
            return []
        sorted_lvls = sorted(levels)
        clusters: List[List[float]] = [[sorted_lvls[0]]]
        for lv in sorted_lvls[1:]:
            ref = float(np.mean(clusters[-1]))
            if abs(lv - ref) / max(ref, 1e-9) <= tolerance_pct:
                clusters[-1].append(lv)
            else:
                clusters.append([lv])

        h = df["high"].values[-140:]
        l = df["low"].values[-140:]
        half_width = max(0.22 * atr, float(df["close"].iloc[-1]) * 0.0018)

        zones: List[Dict[str, Any]] = []
        for cl in clusters:
            mid = float(np.mean(cl))
            z_low = mid - half_width
            z_high = mid + half_width
            # Count how many candles touched/interacted with [z_low, z_high]
            touches = int(np.sum((l <= z_high) & (h >= z_low)))
            zones.append(
                {
                    "mid": round(mid, 6),
                    "low": round(z_low, 6),
                    "high": round(z_high, 6),
                    "pivots_count": len(cl),
                    "touches": touches,
                    "strength": "قوی" if (touches >= 6 or len(cl) >= 3) else "متوسط",
                }
            )
        return zones

    @staticmethod
    def _compute_trendlines(df: pd.DataFrame, atr: float) -> Dict[str, Any]:
        """
        Detects active Diagonal Ascending Support Trendline & Descending Resistance Trendline
        over the most recent 85 candles, plus breakouts/retests on the last 3 candles.
        """
        sub = df.tail(85).reset_index(drop=True)
        n = len(sub)
        if n < 25:
            return {"ascending": None, "descending": None}

        h = sub["high"].values
        l = sub["low"].values
        c = sub["close"].values
        t = sub["timestamp"].values

        # Identify fractal swing highs & swing lows (exclude last 3 candles so breakouts are detected cleanly!)
        swing_highs: List[Tuple[int, float]] = []
        swing_lows: List[Tuple[int, float]] = []
        for i in range(2, n - 3):
            if h[i] == np.max(h[max(0, i - 3) : min(n - 2, i + 4)]):
                swing_highs.append((i, float(h[i])))
            if l[i] == np.min(l[max(0, i - 3) : min(n - 2, i + 4)]):
                swing_lows.append((i, float(l[i])))

        best_desc: Optional[Dict[str, Any]] = None
        best_desc_score = -1e9
        # Pairwise candidate Descending Resistance Trendlines (p2 < p1)
        for idx_a in range(len(swing_highs)):
            for idx_b in range(idx_a + 1, len(swing_highs)):
                i1, p1 = swing_highs[idx_a]
                i2, p2 = swing_highs[idx_b]
                if (i2 - i1) < 8 or p2 >= p1 * 0.9995:
                    continue
                slope = (p2 - p1) / (i2 - i1)
                # Evaluate line across [i1 .. n-4] (prior to breakout window)
                xs = np.arange(i1, n - 3)
                line_vals = p1 + slope * (xs - i1)
                closes_seg = c[i1 : n - 3]
                highs_seg = h[i1 : n - 3]
                # Penalize body closes above resistance line prior to breakout window
                violations = int(np.sum(closes_seg > line_vals + 0.20 * atr))
                if violations > 2:
                    continue
                near_touches = int(np.sum(np.abs(highs_seg - line_vals) <= 0.40 * atr))
                proj_now = p1 + slope * ((n - 1) - i1)
                if proj_now <= 0:
                    continue
                # Prefer recent, well-touched lines close to current price
                prox_penalty = abs(c[-1] - proj_now) / max(atr, 1e-9) * 0.6
                cand_score = near_touches * 5.0 + (i2 / n) * 8.0 - violations * 6.0 - prox_penalty
                if cand_score > best_desc_score:
                    best_desc_score = cand_score
                    prev3_below = bool(np.any(c[-5:-1] <= (p1 + slope * (np.arange(n - 5, n - 1) - i1)) + 0.1 * atr))
                    curr_diff_atr = (c[-1] - proj_now) / max(atr, 1e-9)
                    if curr_diff_atr > 0.18 and prev3_below:
                        status = "BREAKOUT_UP"
                        status_fa = "شکست خط روند نزولی رو به بالا 🚀"
                    elif 0.0 <= curr_diff_atr <= 0.45:
                        status = "RETEST_SUPPORT"
                        status_fa = "پولبک و تست خط روند نزولی شکسته‌شده 🎯"
                    elif -0.55 <= curr_diff_atr < 0.0:
                        status = "RESISTANCE_NEAR"
                        status_fa = "درگیر زیر مقاومت خط روند نزولی 🛑"
                    elif curr_diff_atr > 0.45:
                        status = "ABOVE_BROKEN"
                        status_fa = "تثبیت‌شده بالای خط روند نزولی قبلی"
                    else:
                        status = "BELOW_ACTIVE"
                        status_fa = "زیر خط روند نزولی فعال"

                    best_desc = {
                        "type": "DESCENDING",
                        "t1": int(t[i1]),
                        "p1": round(p1, 6),
                        "t2": int(t[-1]),
                        "p2": round(float(proj_now), 6),
                        "current_price_on_line": round(float(proj_now), 6),
                        "slope_per_bar": round(float(slope), 6),
                        "touches": max(2, near_touches),
                        "status": status,
                        "status_fa": status_fa,
                        "diff_atr": round(float(curr_diff_atr), 2),
                    }

        best_asc: Optional[Dict[str, Any]] = None
        best_asc_score = -1e9
        # Pairwise candidate Ascending Support Trendlines (p2 > p1)
        for idx_a in range(len(swing_lows)):
            for idx_b in range(idx_a + 1, len(swing_lows)):
                i1, p1 = swing_lows[idx_a]
                i2, p2 = swing_lows[idx_b]
                if (i2 - i1) < 8 or p2 <= p1 * 1.0005:
                    continue
                slope = (p2 - p1) / (i2 - i1)
                xs = np.arange(i1, n - 3)
                line_vals = p1 + slope * (xs - i1)
                closes_seg = c[i1 : n - 3]
                lows_seg = l[i1 : n - 3]
                violations = int(np.sum(closes_seg < line_vals - 0.20 * atr))
                if violations > 2:
                    continue
                near_touches = int(np.sum(np.abs(lows_seg - line_vals) <= 0.40 * atr))
                proj_now = p1 + slope * ((n - 1) - i1)
                if proj_now <= 0:
                    continue
                prox_penalty = abs(c[-1] - proj_now) / max(atr, 1e-9) * 0.6
                cand_score = near_touches * 5.0 + (i2 / n) * 8.0 - violations * 6.0 - prox_penalty
                if cand_score > best_asc_score:
                    best_asc_score = cand_score
                    prev3_above = bool(np.any(c[-5:-1] >= (p1 + slope * (np.arange(n - 5, n - 1) - i1)) - 0.1 * atr))
                    curr_diff_atr = (c[-1] - proj_now) / max(atr, 1e-9)
                    if curr_diff_atr < -0.18 and prev3_above:
                        status = "BREAKDOWN_DOWN"
                        status_fa = "شکست خط روند صعودی رو به پایین 📉"
                    elif 0.0 <= curr_diff_atr <= 0.55:
                        status = "BOUNCE_SUPPORT"
                        status_fa = "واکنش حمایتی روی خط روند صعودی 🎯"
                    elif -0.45 <= curr_diff_atr < 0.0:
                        status = "RETEST_RESISTANCE"
                        status_fa = "پولبک از پایین به خط روند صعودی شکسته‌شده 🛑"
                    elif curr_diff_atr > 0.55:
                        status = "ABOVE_ACTIVE"
                        status_fa = "بالای خط روند صعودی معتبر"
                    else:
                        status = "BELOW_BROKEN"
                        status_fa = "زیر خط روند صعودی قبلی"

                    best_asc = {
                        "type": "ASCENDING",
                        "t1": int(t[i1]),
                        "p1": round(p1, 6),
                        "t2": int(t[-1]),
                        "p2": round(float(proj_now), 6),
                        "current_price_on_line": round(float(proj_now), 6),
                        "slope_per_bar": round(float(slope), 6),
                        "touches": max(2, near_touches),
                        "status": status,
                        "status_fa": status_fa,
                        "diff_atr": round(float(curr_diff_atr), 2),
                    }

        return {"ascending": best_asc, "descending": best_desc}

    async def analyze(self, symbol: str, context: Dict[str, Any]) -> AgentReport:
        df: pd.DataFrame = context["df_primary"]
        df_htf: pd.DataFrame = context.get("df_htf", df)
        deriv: Dict[str, Any] = context["derivatives"]

        h = df["high"].values
        l = df["low"].values
        c = df["close"].values
        v = (df["quote_volume"] if "quote_volume" in df else df["volume"]).values

        curr_px = float(c[-1])
        tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
        atr = float(pd.Series(tr).rolling(14).mean().iloc[-1])

        # 1. Detect Swing Highs & Swing Lows
        raw_pivots: List[float] = []
        for i in range(3, len(c) - 3):
            if h[i] == np.max(h[i - 3 : i + 4]):
                raw_pivots.append(float(h[i]))
            if l[i] == np.min(l[i - 3 : i + 4]):
                raw_pivots.append(float(l[i]))

        htf_h = df_htf["high"].values
        htf_l = df_htf["low"].values
        for i in range(2, len(htf_h) - 2):
            if htf_h[i] == np.max(htf_h[i - 2 : i + 3]):
                raw_pivots.append(float(htf_h[i]))
            if htf_l[i] == np.min(htf_l[i - 2 : i + 3]):
                raw_pivots.append(float(htf_l[i]))

        # 2. Direction-aware Fibonacci retracement & extension over the latest 80-bar impulse.
        # If the low happened before the high, the impulse is bullish and retracements
        # are measured downward from the high. If the high came first, vice versa.
        recent_h = h[-80:]
        recent_l = l[-80:]
        hi_rel_idx = int(np.argmax(recent_h))
        lo_rel_idx = int(np.argmin(recent_l))
        swing_hi = float(recent_h[hi_rel_idx])
        swing_lo = float(recent_l[lo_rel_idx])
        fib_diff = max(swing_hi - swing_lo, 1e-9)
        fib_impulse = "BULLISH" if lo_rel_idx < hi_rel_idx else "BEARISH"
        fib_ratios = (0.236, 0.382, 0.500, 0.618, 0.650, 0.786)
        if fib_impulse == "BULLISH":
            fib_levels = {r: swing_hi - r * fib_diff for r in fib_ratios}
            fib_ext_1272 = swing_hi + 0.272 * fib_diff
            fib_ext_1618 = swing_hi + 0.618 * fib_diff
        else:
            fib_levels = {r: swing_lo + r * fib_diff for r in fib_ratios}
            fib_ext_1272 = swing_lo - 0.272 * fib_diff
            fib_ext_1618 = swing_lo - 0.618 * fib_diff

        fib_236 = fib_levels[0.236]
        fib_382 = fib_levels[0.382]
        fib_500 = fib_levels[0.500]
        fib_618 = fib_levels[0.618]
        fib_650 = fib_levels[0.650]
        fib_786 = fib_levels[0.786]
        golden_low, golden_high = sorted((fib_618, fib_650))
        fib_named = {
            "23.6%": fib_236,
            "38.2%": fib_382,
            "50.0%": fib_500,
            "61.8%": fib_618,
            "65.0%": fib_650,
            "78.6%": fib_786,
        }
        nearest_fib_name, nearest_fib_price = min(
            fib_named.items(), key=lambda item: abs(curr_px - item[1])
        )
        nearest_fib_dist_atr = abs(curr_px - nearest_fib_price) / max(atr, 1e-9)
        raw_pivots.extend(
            [
                swing_hi, swing_lo, fib_236, fib_382, fib_500,
                fib_618, fib_650, fib_786, fib_ext_1272, fib_ext_1618,
            ]
        )

        # 3. Volume Profile POC
        bins = np.linspace(float(np.min(l[-100:])), float(np.max(h[-100:])), 25)
        bin_indices = np.clip(np.digitize(c[-100:], bins) - 1, 0, 23)
        vol_hist = np.zeros(24)
        for bi, vol_val in zip(bin_indices, v[-100:]):
            vol_hist[bi] += vol_val
        poc_idx = int(np.argmax(vol_hist))
        poc_price = float((bins[poc_idx] + bins[poc_idx + 1]) / 2.0)
        raw_pivots.append(poc_price)

        # 4. Build S/R Zones (with low/high bands & touch count)
        zones = self._cluster_zones(raw_pivots, df, atr, tolerance_pct=max(0.004, (atr / curr_px) * 0.45))
        sup_zones = sorted([z for z in zones if z["mid"] < curr_px * 0.9992], key=lambda x: x["mid"], reverse=True)
        res_zones = sorted([z for z in zones if z["mid"] > curr_px * 1.0008], key=lambda x: x["mid"])

        half_w = max(0.22 * atr, curr_px * 0.0018)
        while len(sup_zones) < 2:
            base_m = (sup_zones[-1]["mid"] - 1.8 * atr) if sup_zones else (curr_px - 1.5 * atr)
            sup_zones.append({"mid": round(base_m, 6), "low": round(base_m - half_w, 6), "high": round(base_m + half_w, 6), "touches": 2, "strength": "متوسط"})
        while len(res_zones) < 2:
            base_m = (res_zones[-1]["mid"] + 1.8 * atr) if res_zones else (curr_px + 1.5 * atr)
            res_zones.append({"mid": round(base_m, 6), "low": round(base_m - half_w, 6), "high": round(base_m + half_w, 6), "touches": 2, "strength": "متوسط"})

        s1_z, s2_z = sup_zones[0], sup_zones[1]
        r1_z, r2_z = res_zones[0], res_zones[1]
        s1, s2 = s1_z["mid"], s2_z["mid"]
        r1, r2 = r1_z["mid"], r2_z["mid"]

        # 5. Compute Dynamic Diagonal Trendlines
        tls = self._compute_trendlines(df, atr)
        asc_tl = tls["ascending"]
        desc_tl = tls["descending"]

        dist_to_s1_pct = ((curr_px - s1) / curr_px) * 100.0
        dist_to_r1_pct = ((r1 - curr_px) / curr_px) * 100.0
        room_up_atr = (r1_z["low"] - curr_px) / max(atr, 1e-9)
        room_dn_atr = (curr_px - s1_z["high"]) / max(atr, 1e-9)

        score = 0.0
        bullets: List[str] = []

        # Fibonacci confluence is directional and only scores when the confirmed 4H
        # trend and the latest confirmed candle reaction agree. A bare touch is not a signal.
        htf_close_s = df_htf["close"]
        htf_ema21 = float(htf_close_s.ewm(span=21, adjust=False).mean().iloc[-1])
        htf_ema50 = float(htf_close_s.ewm(span=50, adjust=False).mean().iloc[-1])
        htf_px = float(htf_close_s.iloc[-1])
        fib_htf_bull = htf_px > htf_ema21 and htf_ema21 >= htf_ema50 * 0.998
        fib_htf_bear = htf_px < htf_ema21 and htf_ema21 <= htf_ema50 * 1.002
        bullish_reaction = c[-1] > float(df["open"].iloc[-1]) and c[-1] > c[-2]
        bearish_reaction = c[-1] < float(df["open"].iloc[-1]) and c[-1] < c[-2]
        in_golden_pocket = golden_low - 0.12 * atr <= curr_px <= golden_high + 0.12 * atr
        fib_confluence = "NONE"

        if nearest_fib_dist_atr <= 0.30:
            if fib_impulse == "BULLISH" and fib_htf_bull and bullish_reaction:
                fib_points = 22.0 if in_golden_pocket else 16.0
                score += fib_points
                fib_confluence = "BULLISH_CONFIRMED"
                bullets.append(
                    f"همگرایی صعودی فیبوناچی: واکنش تاییدشده به سطح {nearest_fib_name} در ${self.fmt_px(nearest_fib_price)} هم‌جهت با روند ۴ساعته"
                    + (" (ناحیه طلایی 61.8–65%)." if in_golden_pocket else ".")
                )
            elif fib_impulse == "BEARISH" and fib_htf_bear and bearish_reaction:
                fib_points = 22.0 if in_golden_pocket else 16.0
                score -= fib_points
                fib_confluence = "BEARISH_CONFIRMED"
                bullets.append(
                    f"همگرایی نزولی فیبوناچی: ریجکشن تاییدشده از سطح {nearest_fib_name} در ${self.fmt_px(nearest_fib_price)} هم‌جهت با روند ۴ساعته"
                    + (" (ناحیه طلایی 61.8–65%)." if in_golden_pocket else ".")
                )
            else:
                fib_confluence = "TOUCH_UNCONFIRMED"
                bullets.append(
                    f"قیمت نزدیک سطح فیبوناچی {nearest_fib_name} (${self.fmt_px(nearest_fib_price)}) است، اما واکنش هم‌جهت ۴ساعته هنوز تایید نشده؛ بدون امتیاز ورود."
                )

        # S/R Zone reaction scoring
        if curr_px <= s1_z["high"] + 0.3 * atr and c[-1] >= float(df["open"].iloc[-1]):
            score += 26.0
            bullets.append(
                f"واکنش مثبت قیمت به محدوده حمایت S1 (${self.fmt_px(s1_z['low'])} تا ${self.fmt_px(s1_z['high'])} | {s1_z['touches']} برخورد، اعتبار {s1_z['strength']})."
            )
        elif curr_px >= r1_z["low"] - 0.3 * atr and c[-1] <= float(df["open"].iloc[-1]):
            score -= 26.0
            bullets.append(
                f"ریجکشن قیمت از محدوده مقاومت R1 (${self.fmt_px(r1_z['low'])} تا ${self.fmt_px(r1_z['high'])} | {r1_z['touches']} برخورد، اعتبار {r1_z['strength']})."
            )
        else:
            asym = (room_up_atr - room_dn_atr) / max(abs(room_up_atr) + abs(room_dn_atr) + 0.5, 1e-9)
            score += asym * 28.0
            bullets.append(
                f"محدوده حمایت اول: ${self.fmt_px(s1_z['low'])}–${self.fmt_px(s1_z['high'])} ({s1_z['touches']} برخورد) | محدوده مقاومت اول: ${self.fmt_px(r1_z['low'])}–${self.fmt_px(r1_z['high'])} ({r1_z['touches']} برخورد)."
            )

        # Diagonal Trendline scoring (high priority for trader!)
        if desc_tl:
            st = desc_tl["status"]
            tl_px = desc_tl["current_price_on_line"]
            if st == "BREAKOUT_UP":
                score += 28.0
                bullets.insert(0, f"شکست خط روند نزولی داینامیک در سطح ${self.fmt_px(tl_px)} رو به بالا ({desc_tl['touches']} برخورد معتبر)!")
            elif st == "RETEST_SUPPORT":
                score += 22.0
                bullets.insert(0, f"پولبک موفق روی خط روند نزولی شکسته‌شده در ${self.fmt_px(tl_px)}!")
            elif st == "RESISTANCE_NEAR":
                score -= 20.0
                bullets.append(f"خط روند نزولی در ${self.fmt_px(tl_px)} به عنوان سد مقاومتی فعال عمل می‌کند.")
            else:
                bullets.append(f"خط روند نزولی در تراز ${self.fmt_px(tl_px)}: {desc_tl['status_fa']}.")

        if asc_tl:
            st = asc_tl["status"]
            tl_px = asc_tl["current_price_on_line"]
            if st == "BREAKDOWN_DOWN":
                score -= 28.0
                bullets.insert(0, f"شکست خط روند صعودی داینامیک در سطح ${self.fmt_px(tl_px)} رو به پایین ({asc_tl['touches']} برخورد)!")
            elif st == "BOUNCE_SUPPORT":
                score += 22.0
                bullets.insert(0, f"حمایت دقیق قیمت روی خط روند صعودی در تراز ${self.fmt_px(tl_px)}!")
            elif st == "RETEST_RESISTANCE":
                score -= 20.0
                bullets.append(f"پولبک از پایین به خط روند صعودی شکسته‌شده در ${self.fmt_px(tl_px)} (مقاومت داینامیک).")
            else:
                bullets.append(f"خط روند صعودی در تراز ${self.fmt_px(tl_px)}: {asc_tl['status_fa']}.")

        # POC relation
        if curr_px > poc_price * 1.001:
            score += 10.0
        elif curr_px < poc_price * 0.999:
            score -= 10.0

        score = round(self.clamp(score), 1)
        direction = self.score_to_direction(score, threshold=14.0)
        confidence = round(min(95.0, 52.0 + abs(score) * 0.45), 1)

        tl_brief = ""
        if desc_tl and desc_tl["status"] in ("BREAKOUT_UP", "RETEST_SUPPORT", "RESISTANCE_NEAR"):
            tl_brief = f" | {desc_tl['status_fa']}"
        elif asc_tl and asc_tl["status"] in ("BREAKDOWN_DOWN", "BOUNCE_SUPPORT", "RETEST_RESISTANCE"):
            tl_brief = f" | {asc_tl['status_fa']}"

        summary = (
            f"محدوده حمایت [${self.fmt_px(s1_z['low'])}–${self.fmt_px(s1_z['high'])}] و مقاومت [${self.fmt_px(r1_z['low'])}–${self.fmt_px(r1_z['high'])}]{tl_brief} (امتیاز {score:+.0f})."
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
                "s1": round(s1, 6),
                "s2": round(s2, 6),
                "r1": round(r1, 6),
                "r2": round(r2, 6),
                "s1_zone": s1_z,
                "s2_zone": s2_z,
                "r1_zone": r1_z,
                "r2_zone": r2_z,
                "ascending_tl": asc_tl,
                "descending_tl": desc_tl,
                "poc_price": round(poc_price, 6),
                "fib_236": round(fib_236, 6),
                "fib_382": round(fib_382, 6),
                "fib_500": round(fib_500, 6),
                "fib_618": round(fib_618, 6),
                "fib_650": round(fib_650, 6),
                "fib_786": round(fib_786, 6),
                "fibonacci": {
                    "impulse_direction": fib_impulse,
                    "swing_low": round(swing_lo, 6),
                    "swing_high": round(swing_hi, 6),
                    "levels": {name: round(px, 6) for name, px in fib_named.items()},
                    "golden_pocket_low": round(golden_low, 6),
                    "golden_pocket_high": round(golden_high, 6),
                    "extension_1.272": round(fib_ext_1272, 6),
                    "extension_1.618": round(fib_ext_1618, 6),
                    "nearest_level": nearest_fib_name,
                    "nearest_price": round(nearest_fib_price, 6),
                    "distance_atr": round(nearest_fib_dist_atr, 3),
                    "in_golden_pocket": in_golden_pocket,
                    "confluence": fib_confluence,
                },
                "room_up_atr": round(room_up_atr, 2),
                "room_dn_atr": round(room_dn_atr, 2),
                "dist_to_s1_pct": round(dist_to_s1_pct, 2),
                "dist_to_r1_pct": round(dist_to_r1_pct, 2),
            },
        )
