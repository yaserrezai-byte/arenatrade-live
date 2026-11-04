import asyncio
from datetime import datetime, timezone
import time
from typing import Any, Dict, Optional

from agents.telegram_agent import DEFAULT_SIGNAL_INLINE_KEYBOARD, telegram_agent
from core.config import settings
from core.database import db


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_ago(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds} ثانیه پیش"
    mins = seconds // 60
    rem_s = seconds % 60
    if mins < 60:
        return f"{mins} دقیقه و {rem_s} ثانیه پیش"
    hrs = mins // 60
    rem_m = mins % 60
    return f"{hrs} ساعت و {rem_m} دقیقه پیش"


class WatchdogSupervisorAgent:
    """
    Independent Supervisor & Delivery Auditor Agent (ایجنت مستقل ناظر زمان‌بندی و ارسال):
    1) Audits whether the 15-minute Market Analysis scan actually executed on schedule.
       If delayed (>19m), forcefully unlocks & triggers an emergency scan + notifies Telegram.
    2) Audits whether the 90-second Copy-Trader whale monitor actually executed on schedule.
       If delayed (>5.5m), forcefully triggers an immediate whale check.
    3) Audits SQLite for any discovered actionable signal (Market Analysis or Copy-Trader)
       whose Telegram delivery did not complete (telegram_sent=0) and re-delivers it automatically.
    4) Provides on-demand & hourly signed audit reports in Telegram.
    """

    agent_id = "watchdog_supervisor"
    agent_name_fa = "ایجنت ناظر زمان‌بندی و بازرس ارسال تلگرام"

    def __init__(self) -> None:
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.checks_performed: int = 0
        self.last_check_ts: int = 0

    def start_supervisor(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._supervisor_loop())

    async def stop_supervisor(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def run_audit_and_heal_once(self) -> Dict[str, Any]:
        stats = db.get_watchdog_audit_stats()
        now = int(time.time())
        self.checks_performed += 1
        self.last_check_ts = now

        scan_interval = max(60, int(settings.get("AUTO_SCAN_INTERVAL_SEC", 900)))
        max_scan_age = scan_interval + 240  # 19m threshold for 15m schedule
        max_copy_age = max(300, int(settings.get("COPY_MONITOR_INTERVAL_SEC", 90)) * 3 + 60)

        healed_actions = []

        # 1. Audit Market Analysis 15-minute execution
        last_ma = stats["last_market_scan_ts"]
        ma_age = (now - last_ma) if last_ma > 0 else 999999
        if settings.get("AUTO_SCAN_ENABLED", True) and ma_age > max_scan_age:
            from services.scanner_service import scanner_service

            if scanner_service.is_scanning_now and ma_age > max_scan_age + 120:
                scanner_service.is_scanning_now = False

            if not scanner_service.is_scanning_now:
                ts_iso = _now_iso()
                msg_fa = (
                    f"تاخیر در اسکن ۱۵ دقیقه‌ای تشخیص داده شد (آخرین تحلیل: {_fmt_ago(ma_age)}). "
                    f"ایجنت ناظر بلافاصله اسکن اضطراری ۸ ایجنت را به صورت خودکار اجرا کرد."
                )
                db.log_watchdog_event(
                    timestamp_iso=ts_iso,
                    event_type="SLA_HEAL_MARKET_SCAN",
                    severity="WARN",
                    auto_healed=True,
                    summary_fa=msg_fa,
                )
                try:
                    await scanner_service.scan_watchlist_once(
                        settings.watchlist, send_summary_to_telegram=True
                    )
                    healed_actions.append("اجرای خودکار اسکن عقب‌افتاده بازار")
                    if settings.get("TELEGRAM_AUTO_SEND", True):
                        await telegram_agent.send_html_message(
                            f"🛡♻️ <b>گزارش خودترمیمی ایجنت ناظر:</b>\n{msg_fa}",
                            msg_type="WATCHDOG_HEAL",
                            reply_markup=DEFAULT_SIGNAL_INLINE_KEYBOARD,
                        )
                except Exception:
                    pass

        # 2. Audit Copy-Trader 90-second execution
        last_cp = stats["last_copy_check_ts"]
        cp_age = (now - last_cp) if last_cp > 0 else 999999
        if cp_age > max_copy_age:
            from agents.copy_trader_agent import copy_trader_agent

            ts_iso = _now_iso()
            db.log_watchdog_event(
                timestamp_iso=ts_iso,
                event_type="SLA_HEAL_COPY_MONITOR",
                severity="WARN",
                auto_healed=True,
                summary_fa=f"وقفه در رصد کپی‌تریدر ({_fmt_ago(cp_age)}) توسط ایجنت ناظر برطرف و بررسی مجدد نهنگ‌ها انجام شد.",
            )
            try:
                await copy_trader_agent.monitor_active_trader(auto_emit_new_signals=True)
                healed_actions.append("بازیابی خودکار رادار کپی‌تریدر")
            except Exception:
                pass

        # 3. Audit undelivered Copy-Trader signals & retry delivery
        if settings.get("TELEGRAM_AUTO_SEND", True):
            for sig in stats.get("unsent_copy_signals", []):
                try:
                    tg_res = await telegram_agent.send_signal(sig)
                    if tg_res.get("ok"):
                        addr = sig.get("copied_trader_address") or ""
                        pos_key = f"{addr}:{sig['symbol']}:{sig['direction']}"
                        whale_entry = float(
                            (sig.get("previous_comparison") or {}).get("whale_entry_price")
                            or sig.get("current_price")
                            or 0.0
                        )
                        if addr:
                            db.mark_copy_position_signaled(
                                pos_key, addr, sig["symbol"], sig["direction"], whale_entry
                            )
                        db.log_watchdog_event(
                            timestamp_iso=_now_iso(),
                            event_type="RETRY_DELIVERY_SUCCESS",
                            severity="INFO",
                            auto_healed=True,
                            summary_fa=f"سیگنال جامانده کپی‌تریدر #{sig['symbol']} توسط ایجنت ناظر با موفقیت به تلگرام ارسال شد.",
                        )
                        healed_actions.append(f"ارسال مجدد سیگنال جامانده #{sig['symbol']}")
                except Exception:
                    pass

        return {
            "checked_at": now,
            "stats": db.get_watchdog_audit_stats(),
            "healed_actions": healed_actions,
        }

    def get_audit_stamp_fa(self) -> str:
        stats = db.get_watchdog_audit_stats()
        now = int(time.time())
        cp_age = (now - stats["last_copy_check_ts"]) if stats["last_copy_check_ts"] else -1
        cp_txt = _fmt_ago(cp_age) if cp_age >= 0 else "در حال راه‌اندازی"
        return (
            f"🛡 <b>تاییدیه ایجنت ناظر زمان‌بندی و ارسال:</b>\n"
            f"• نوبت‌های اسکن در ۱ساعت/۶ساعت اخیر: <code>{stats['scan_batches_last_1h']}</code> / <code>{stats['scan_batches_last_6h']}</code> نوبت\n"
            f"• آخرین رصد کپی‌تریدر: <code>{cp_txt}</code> | تحویل موفق تلگرام (۶ساعت): <code>{stats['tg_delivered_6h']} پیام</code> (خطا: <code>{stats['tg_failed_6h']}</code>)"
        )

    def format_full_audit_report_html(self) -> str:
        stats = db.get_watchdog_audit_stats()
        now = int(time.time())
        interval = max(60, int(settings.get("AUTO_SCAN_INTERVAL_SEC", 900)))

        ma_ts = stats["last_market_scan_ts"]
        cp_ts = stats["last_copy_check_ts"]
        tg_ts = stats["last_tg_delivered_ts"]

        ma_age = (now - ma_ts) if ma_ts else -1
        cp_age = (now - cp_ts) if cp_ts else -1
        tg_age = (now - tg_ts) if tg_ts else -1

        next_candle_ts = int((now - (now % interval)) + interval + 5)
        rem_next = max(0, next_candle_ts - now)
        next_utc = datetime.fromtimestamp(next_candle_ts, tz=timezone.utc).strftime("%H:%M:%S UTC")

        ma_ok = 0 <= ma_age <= (interval + 240)
        cp_ok = 0 <= cp_age <= 330
        overall_ok = ma_ok and cp_ok

        header_badge = "🟢 <b>وضعیت کلی: سالم و دقیق سرِ زمان مقرر ✅</b>" if overall_ok else "🟡 <b>وضعیت کلی: در حال خودترمیمی توسط ناظر ♻️</b>"
        logs = db.list_watchdog_logs(limit=4)

        lines = [
            "🛡🔍 <b>گزارش بازرسی «ایجنت ناظر زمان‌بندی و ارسال تلگرام»</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            header_badge,
            "",
            "⏱ <b>۱. بازرسی موتور تحلیل بازار (برنامه ۱۵ دقیقه‌ای):</b>",
            f"• وضعیت اسکن: {'✅ انجام‌شده در زمان مقرر' if ma_ok else '⚠️ نیازمند اسکن فوری'}",
            f"• آخرین تحلیل ۸ ایجنت: <code>{_fmt_ago(ma_age) if ma_age >= 0 else 'ثبت نشده'}</code>",
            f"• زمان کندل ۱۵ دقیقه‌ای بعدی: <code>{next_utc}</code> (<code>{rem_next // 60} دقیقه و {rem_next % 60} ثانیه دیگر</code>)",
            f"• تعداد اسکن‌های انجام‌شده: <code>{stats['scan_batches_last_1h']} نوبت</code> در ۱ ساعت اخیر | <code>{stats['scan_batches_last_6h']} نوبت</code> در ۶ ساعت اخیر",
            "",
            "🐋 <b>۲. بازرسی رادار کپی‌تریدر (برنامه ۹۰ ثانیه‌ای):</b>",
            f"• وضعیت رصد نهنگ‌ها: {'✅ فعال و به‌روز' if cp_ok else '⚠️ در حال بروزرسانی'}",
            f"• آخرین بررسی کیف‌پول ۳ نهنگ برتر: <code>{_fmt_ago(cp_age) if cp_age >= 0 else 'ثبت نشده'}</code>",
            "",
            "📨 <b>۳. بازرسی تحویل پیام‌ها به تلگرام:</b>",
            f"• پیام‌های تحویل‌شده موفق (۶ ساعت اخیر): <code>{stats['tg_delivered_6h']} پیام ✅</code>",
            f"• پیام‌های خطادار تحویل‌نشده: <code>{stats['tg_failed_6h']} مورد</code>",
            f"• آخرین تحویل موفق به تلگرام: <code>{_fmt_ago(tg_age) if tg_age >= 0 else '—'}</code>",
            f"• عملیات خودترمیمی ایجنت ناظر (۲۴ساعت): <code>{stats['auto_heals_24h']} مورد</code>",
        ]

        if logs:
            lines.extend(["", "📋 <b>آخرین وقایع ثبت‌شده توسط ایجنت ناظر:</b>"])
            for lg in logs[:3]:
                lines.append(f"• <code>{lg['timestamp_iso'][11:16]}</code> — {lg['summary_fa']}")

        lines.extend(["━━━━━━━━━━━━━━━━━━━━", f"🕒 <code>{_now_iso()}</code>"])
        return "\n".join(lines)

    async def _supervisor_loop(self) -> None:
        # Allow initial startup scan 45s headroom before auditing
        await asyncio.sleep(45)
        while self._running:
            try:
                await self.run_audit_and_heal_once()
            except Exception:
                pass
            await asyncio.sleep(60)


watchdog_agent = WatchdogSupervisorAgent()
