import asyncio
from datetime import datetime, timezone
import time
from typing import Any, Dict, List, Optional

from agents.copy_trader_agent import copy_trader_agent
from agents.orchestrator_agent import orchestrator_agent
from core.config import settings
from core.database import db


def _fmt_px(px: float) -> str:
    if px >= 1000:
        return f"{px:,.2f}"
    if px >= 1:
        return f"{px:,.4f}".rstrip("0").rstrip(".")
    return f"{px:.6f}".rstrip("0").rstrip(".")


class MarketScannerService:
    """
    Manages:
    1) Continuous 15-Minute Watchlist Scanning across all 8 pure Market Analysis Agents
       (wall-clock aware so VM sleep/resume never stalls the loop)
    2) Real-time Independent Copy-Trader 2-Strike Monitoring & Auto-Rotation
    3) Automatic Telegram delivery of BOTH:
       - Standalone Market Analysis Signals (for actionable setups)
       - 15-Minute Watchlist Scan Report (so every 15m scan is visible in Telegram)
    """

    def __init__(self) -> None:
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._last_sent_info: Dict[str, Dict[str, float]] = {}
        self.last_scan_ts: Optional[int] = None
        self.last_scan_results: List[Dict[str, Any]] = []
        self.is_scanning_now: bool = False
        self.scan_started_ts: Optional[int] = None
        self.last_scan_error_count: int = 0

    async def initialize(self) -> None:
        try:
            await copy_trader_agent.refresh_leaderboard_roster(force_reseed=False)
        except Exception:
            pass

    def _format_auto_scan_report_html(self, results: List[Dict[str, Any]], sent_symbols: List[str]) -> str:
        ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        expected = len(settings.watchlist)
        lines = [
            "⏱📡 <b>گزارش اسکن خودکار ۱۵ دقیقه‌ای بازار فیوچرز (۸ ایجنت تحلیل)</b>",
            f"🕒 زمان اسکن: <code>{ts_str}</code> | پوشش: <code>{len(results)}/{expected} ارز</code>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        if len(results) < expected:
            lines.append(f"⚠️ <b>هشدار کیفیت اجرا:</b> تحلیل {expected - len(results)} ارز کامل نشد؛ ناظر در چرخه بعدی مجدداً تلاش می‌کند.")
        icon_map = {"LONG": "🟢", "SHORT": "🔴", "NEUTRAL": "⚪️"}

        for r in results:
            sig = r["signal"]
            sym = sig["symbol"]
            d = sig["direction"]
            ic = icon_map.get(d, "⚪️")
            px = _fmt_px(float(sig["current_price"]))
            conf = float(sig["confidence"])
            score = float(sig["composite_score"])

            # Extract S/R, Trendline, Money Flow, RSI/MA, and Historical Comparison highlights
            reps = {rep["agent_id"]: rep.get("metrics", {}) for rep in sig.get("agent_reports", [])}
            mf = reps.get("money_flow", {})
            ind = reps.get("indicators", {})
            sr = reps.get("support_resistance", {})

            net_1h = float(mf.get("net_inflow_1h_usd", 0.0)) / 1e6
            flow_str = f"{net_1h:+.2f}M USD"
            rsi_1h = ind.get("rsi_14")
            rsi_str = f"{float(rsi_1h):.1f}" if rsi_1h is not None else "—"
            ma_stack = str(ind.get("ma_status_fa", ""))

            s1 = sr.get("s1")
            r1 = sr.get("r1")
            sr_str = ""
            if s1 and r1:
                sr_str = f" | S1:<code>{_fmt_px(float(s1))}</code> R1:<code>{_fmt_px(float(r1))}</code>"

            comp = sig.get("previous_comparison") or {}
            delta_str = ""
            if comp.get("has_previous"):
                dp = float(comp.get("price_change_pct", 0.0))
                ds = float(sig["composite_score"]) - float(comp.get("previous_score", 0.0))
                delta_str = f" | Δقیمت:<code>{dp:+.2f}%</code> Δامتیاز:<code>{ds:+.1f}</code>"

            act_badge = " 🎯<b>[سیگنال مستقل ارسال شد]</b>" if sym in sent_symbols else (
                " ✅[معتبر]" if sig.get("actionable") else ""
            )

            lines.append(
                f"{ic} <b>#{sym}</b> | جهت: <code>{d}</code> | اطمینان الگوریتمی: <code>{conf:.0f}/100</code> | امتیاز: <code>{score:+.1f}</code>{act_badge}\n"
                f"💵 قیمت: <code>{px} USDT</code> | جریان خالص ۱ساعته: <code>{flow_str}</code>\n"
                f"📐 RSI-1h: <code>{rsi_str}</code> | {ma_stack}{sr_str}{delta_str}"
            )

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        if sent_symbols:
            lines.append(f"✅ سیگنال‌های کامل ورود و حد ضرر برای: <b>{', '.join('#' + s for s in sent_symbols)}</b> جداگانه ارسال شد.")
        else:
            lines.append("ℹ️ در این کندل ۱۵ دقیقه‌ای سیگنال ورود کم‌ریسک جدیدی خارج از کول‌داون فعال نشد (بازار تحت رصد مستمر است).")
        try:
            from agents.watchdog_agent import watchdog_agent

            lines.extend(["", watchdog_agent.get_audit_stamp_fa()])
        except Exception:
            pass
        return "\n".join(lines)

    async def scan_watchlist_once(
        self,
        symbols: Optional[List[str]] = None,
        send_summary_to_telegram: bool = True,
    ) -> List[Dict[str, Any]]:
        if self.is_scanning_now:
            return self.last_scan_results

        self.is_scanning_now = True
        self.scan_started_ts = int(time.time())
        coins = symbols or settings.watchlist
        results: List[Dict[str, Any]] = []
        sent_symbols: List[str] = []
        sem = asyncio.Semaphore(3)

        async def _scan_one(c: str) -> Optional[Dict[str, Any]]:
            async with sem:
                try:
                    res = await asyncio.wait_for(
                        orchestrator_agent.analyze_symbol(c, send_to_telegram=False),
                        timeout=120.0,
                    )
                    sig = res["signal"]
                    key = f"{sig['symbol']}:{sig['direction']}"
                    now = time.time()
                    prev = self._last_sent_info.get(key)
                    if not prev:
                        db_prev = db.get_last_sent_market_signal(
                            sig["symbol"], sig["direction"], exclude_signal_id=sig["signal_id"]
                        )
                        if db_prev:
                            prev = {
                                "ts": float(db_prev["created_at"]),
                                "price": float(db_prev["current_price"] or 0.0),
                            }
                    cur_px = float(sig.get("current_price") or 0.0)
                    px_shift_pct = (
                        abs(cur_px - prev["price"]) / prev["price"] * 100.0
                        if prev and prev.get("price", 0) > 0
                        else 999.0
                    )
                    cooldown_passed = (not prev) or ((now - prev["ts"]) > 2600) or (px_shift_pct >= 0.7)

                    if sig["actionable"] and settings.get("TELEGRAM_AUTO_SEND", True) and cooldown_passed:
                        from agents.telegram_agent import telegram_agent

                        tg = await telegram_agent.send_signal(sig)
                        res["telegram_result"] = tg
                        if tg.get("ok"):
                            sig["telegram_sent"] = True
                            db.mark_signal_telegram_sent(sig["signal_id"])
                            sent_symbols.append(sig["symbol"])
                            self._last_sent_info[key] = {"ts": now, "price": cur_px}
                    return res
                except Exception:
                    return None

        try:
            raw_list = await asyncio.gather(*[_scan_one(c) for c in coins])
            results = [r for r in raw_list if r is not None]
            self.last_scan_error_count = max(0, len(coins) - len(results))
            results.sort(key=lambda x: x["signal"]["confidence"], reverse=True)
            self.last_scan_results = results
            self.last_scan_ts = int(time.time())

            if (
                results
                and send_summary_to_telegram
                and settings.get("TELEGRAM_AUTO_SEND", True)
                and settings.get("SEND_SCAN_SUMMARY_REPORT", True)
            ):
                from agents.telegram_agent import DEFAULT_SIGNAL_INLINE_KEYBOARD, telegram_agent

                summary_html = self._format_auto_scan_report_html(results, sent_symbols)
                await telegram_agent.send_html_message(
                    summary_html,
                    msg_type="AUTO_SCAN_15M",
                    reply_markup=DEFAULT_SIGNAL_INLINE_KEYBOARD,
                )

            return results
        finally:
            self.is_scanning_now = False
            self.scan_started_ts = None

    async def _sleep_until_wallclock(self, target_ts: float) -> None:
        """Wall-clock aware sleep so container pause/resume never freezes scheduling."""
        while self._running and time.time() < target_ts:
            rem = target_ts - time.time()
            await asyncio.sleep(min(3.0, max(0.5, rem)))

    async def _watchlist_loop(self) -> None:
        await asyncio.sleep(3)
        if settings.get("AUTO_SCAN_ENABLED", True):
            should_scan_immediately = True
            try:
                recent = db.get_last_sent_market_signal("BTC", "SHORT") or db.get_last_sent_market_signal("BTC", "LONG")
                with db._lock, db._connect() as conn:
                    row = conn.execute(
                        "SELECT MAX(created_at) AS last_ts FROM signals WHERE signal_source = 'MARKET_ANALYSIS'"
                    ).fetchone()
                    if row and row["last_ts"] and (time.time() - float(row["last_ts"])) < 720:
                        should_scan_immediately = False
            except Exception:
                pass

            if should_scan_immediately:
                try:
                    await self.scan_watchlist_once(settings.watchlist, send_summary_to_telegram=True)
                except Exception:
                    pass

        while self._running:
            interval = max(60, int(settings.get("AUTO_SCAN_INTERVAL_SEC", 900)))
            now = time.time()
            if settings.get("SCAN_SYNC_CANDLE_CLOSE", True):
                target_ts = (now - (now % interval)) + interval + 5.0
            else:
                target_ts = now + float(interval)

            await self._sleep_until_wallclock(target_ts)
            if not self._running:
                break

            if settings.get("AUTO_SCAN_ENABLED", True):
                try:
                    await self.scan_watchlist_once(settings.watchlist, send_summary_to_telegram=True)
                except Exception:
                    pass

    async def _copy_monitor_loop(self) -> None:
        await asyncio.sleep(6)
        while self._running:
            try:
                await copy_trader_agent.monitor_active_trader()
            except Exception:
                pass
            interval = max(30, int(settings.get("COPY_MONITOR_INTERVAL_SEC", 90)))
            await self._sleep_until_wallclock(time.time() + float(interval))

    def start_background_loops(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._watchlist_loop()),
            asyncio.create_task(self._copy_monitor_loop()),
        ]

    async def stop_background_loops(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()


scanner_service = MarketScannerService()
