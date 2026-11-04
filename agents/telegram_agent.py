from datetime import datetime, timezone
import asyncio
import re
from typing import Any, Dict, Optional
import httpx

from core.config import settings
from core.database import db


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_price(px: float) -> str:
    if px >= 1000:
        return f"{px:,.2f}"
    if px >= 1:
        return f"{px:,.4f}".rstrip("0").rstrip(".")
    return f"{px:.6f}".rstrip("0").rstrip(".")


DEFAULT_SIGNAL_INLINE_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🧠 تحلیل مستقل BTC", "callback_data": "sig:BTC"},
            {"text": "🧠 تحلیل مستقل ETH", "callback_data": "sig:ETH"},
            {"text": "🧠 تحلیل مستقل SOL", "callback_data": "sig:SOL"},
        ],
        [
            {"text": "🐋 سیگنال مستقل کپی‌تریدر", "callback_data": "cmd:copysig"},
            {"text": "🐋 وضعیت نهنگ فعال", "callback_data": "cmd:copy"},
        ],
        [
            {"text": "📡 اسکن کل واچ‌لیست", "callback_data": "cmd:scan"},
            {"text": "🛡 گزارش ایجنت ناظر", "callback_data": "cmd:watchdog"},
        ],
    ]
}


class TelegramNotifierAgent:
    """
    Formats institutional Persian Futures Signals & Copy-Trader Rotation Alerts
    and delivers them to the configured Telegram Channel via Bot API.
    """

    agent_id = "telegram_notifier"
    agent_name_fa = "ایجنت مخابره کانال تلگرام"

    def format_signal_message(self, sig: Dict[str, Any]) -> str:
        if sig.get("signal_source") == "COPY_TRADER":
            return self.format_copy_trade_message(sig)
        return self.format_market_analysis_message(sig)

    def format_copy_trade_message(self, sig: Dict[str, Any]) -> str:
        direction = sig["direction"]
        header_icon = "🐋🟢" if direction == "LONG" else "🐋🔴"
        dir_fa = "LONG (خرید تعهدی)" if direction == "LONG" else "SHORT (فروش تعهدی)"
        meta = sig.get("previous_comparison") or {}  # holds whale metadata on copy signals

        whale_name = sig.get("copied_trader_label") or "نهنگ برتر لیدربورد"
        whale_addr = sig.get("copied_trader_address") or ""
        short_addr = f"{whale_addr[:8]}...{whale_addr[-4:]}" if len(whale_addr) > 12 else whale_addr
        whale_eq = float(meta.get("whale_equity_usd", 0.0))
        whale_roi = float(meta.get("whale_roi_month_pct", 0.0))
        whale_wr = float(meta.get("whale_win_rate_pct", 0.0))
        whale_pos_usd = float(meta.get("whale_position_value_usd", 0.0))
        whale_entry = float(meta.get("whale_entry_price", sig["current_price"]))
        whale_lev = int(meta.get("whale_leverage", sig["recommended_leverage"]))
        whale_upnl = float(meta.get("whale_unrealized_pnl_usd", 0.0))
        whale_liq = meta.get("whale_liquidation_price")
        consec = int(meta.get("consecutive_losses", 0))
        max_l = int(meta.get("max_allowed_losses", 2))

        lines = [
            f"{header_icon} <b>سیگنال مستقل کپی‌تریدر | #{sig['symbol']}USDT</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            "📡 <b>بخش:</b> <code>کپی مستقیم از تریدر موفق (مستقل از تحلیل تکنیکال)</code>",
            f"👤 <b>تریدر مرجع فعال:</b> <code>{whale_name}</code> (<code>{short_addr}</code>)",
            f"🏆 <b>کارنامه تاریخی نهنگ (تضمین آینده نیست):</b> موجودی <code>${whale_eq:,.0f}</code> | بازدهی ماه اخیر <code>{whale_roi:+.1f}%</code> | وین‌ریت نمونه معاملات اخیر <code>{whale_wr:.1f}%</code>",
            f"🛡 <b>وضعیت خطای متوالی تریدر:</b> <code>{consec} / {max_l}</code> (تعویض خودکار در صورت {max_l} خطای پیاپی)",
            "",
            "🐋 <b>جزئیات پوزیشن بازِ تریدر مرجع:</b>",
            f"📌 <b>جهت پوزیشن نهنگ:</b> <code>{dir_fa}</code>",
            f"💵 <b>حجم پوزیشن نهنگ:</b> <code>${whale_pos_usd:,.0f}</code> (اهرم نهنگ: <code>{whale_lev}x</code>)",
            f"🎯 <b>میانگین ورود نهنگ:</b> <code>${_fmt_price(whale_entry)}</code> | سود/ضرر فعلی: <code>{whale_upnl:+,.0f}$</code>",
        ]
        if whale_liq:
            lines.append(f"⚠️ <b>قیمت لیکویید نهنگ:</b> <code>${_fmt_price(float(whale_liq))}</code>")

        lines.extend(
            [
                "",
                "💰 <b>سطوح پیشنهادی اجرای کپی برای شما:</b>",
                f"🔹 <b>قیمت لحظه‌ای بازار:</b> <code>${_fmt_price(sig['current_price'])}</code>",
                f"📥 <b>محدوده مناسب ورود:</b> <code>${_fmt_price(sig['entry_low'])}</code> — <code>${_fmt_price(sig['entry_high'])}</code>",
                f"⚡️ <b>سقف اهرم الگوریتمی اولیه (نه تضمین ایمنی):</b> <code>{sig['recommended_leverage']}x ({sig.get('margin_mode', 'ISOLATED')})</code>",
                f"🎯 <b>تارگت اول (TP1):</b> <code>${_fmt_price(sig['tp1'])}</code>",
                f"🎯 <b>تارگت دوم (TP2):</b> <code>${_fmt_price(sig['tp2'])}</code>",
                f"🎯 <b>تارگت سوم (TP3):</b> <code>${_fmt_price(sig['tp3'])}</code>",
                f"🛑 <b>حد ضرر محافظتی (SL):</b> <code>${_fmt_price(sig['stop_loss'])}</code>",
                "━━━━━━━━━━━━━━━━━━━━",
                f"🕒 <code>{sig['timestamp_iso']}</code>",
            ]
        )
        return "\n".join(lines)

    def format_market_analysis_message(self, sig: Dict[str, Any]) -> str:
        direction = sig["direction"]
        risk_meta = sig.get("previous_comparison") or {}
        target_basis = risk_meta.get("target_basis") or {}
        if direction == "LONG":
            header_icon = "🧠🟢"
            dir_fa = "LONG (خرید تعهدی)"
        elif direction == "SHORT":
            header_icon = "🧠🔴"
            dir_fa = "SHORT (فروش تعهدی)"
        else:
            header_icon = "🧠⚪️"
            dir_fa = "NEUTRAL (عدم ورود / رصد بازار)"

        lines = [
            f"{header_icon} <b>سیگنال مستقل تحلیل بازار فیوچرز | #{sig['symbol']}USDT</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            "📡 <b>بخش:</b> <code>تحلیل خالص ۸ ایجنت تخصصی بازار (مستقل از کپی‌تریدر)</code>",
            f"📌 <b>جهت پوزیشن:</b> <code>{dir_fa}</code>",
            f"⏱ <b>تایم‌فریم مرجع:</b> <code>{sig['timeframe']}</code> (اسکن ۱۵ دقیقه‌ای)",
            f"🎯 <b>امتیاز اطمینان الگوریتمی (هنوز کالیبره‌نشده به احتمال برد):</b> <code>{sig['confidence']:.1f}/100</code> (برآیند: <code>{sig['composite_score']:+.1f}</code>)",
        ]

        lines.extend(
            [
                "",
                "💰 <b>سطوح معاملاتی و مدیریت سفارش:</b>",
                f"🔹 <b>قیمت فعلی بازار:</b> <code>${_fmt_price(sig['current_price'])}</code>",
                f"📥 <b>محدوده ورود (Entry):</b> <code>${_fmt_price(sig['entry_low'])}</code> — <code>${_fmt_price(sig['entry_high'])}</code>",
                f"⚡️ <b>سقف اهرم الگوریتمی اولیه (پیش از صحت‌سنجی):</b> <code>{sig['recommended_leverage']}x ({sig.get('margin_mode', 'ISOLATED')})</code>",
                f"🎯 <b>تارگت اول (TP1 - سیو سود):</b> <code>${_fmt_price(sig['tp1'])}</code>",
                f"🎯 <b>تارگت دوم (TP2 - اصلی):</b> <code>${_fmt_price(sig['tp2'])}</code>",
                f"🎯 <b>تارگت سوم (TP3 - موج‌سواری):</b> <code>${_fmt_price(sig['tp3'])}</code>",
                f"🛑 <b>حد ضرر الزامی (SL):</b> <code>${_fmt_price(sig['stop_loss'])}</code>",
                f"⚖️ <b>R:R خالص محافظه‌کارانه تا TP2:</b> <code>1 : {sig['risk_reward_tp2']:.2f}</code> | از بدترین ورود و پس از هزینه تقریبی <code>{risk_meta.get('estimated_round_trip_cost_pct', 0.18):.2f}%</code>",
                f"🧱 <b>مبنای تارگت‌ها:</b> <code>TP1={target_basis.get('tp1', '—')} | TP2={target_basis.get('tp2', '—')} | TP3={target_basis.get('tp3', '—')}</code> | ریسک مجاز: <code>{sig.get('risk_pct', 1.5)}%</code>",
            ]
        )

        # Extract Money Flow & Indicator metrics for explicit Telegram display
        mf_m = {}
        ind_m = {}
        for r in sig.get("agent_reports", []):
            if r.get("agent_id") == "money_flow":
                mf_m = r.get("metrics", {})
            elif r.get("agent_id") == "indicators":
                ind_m = r.get("metrics", {})

        if mf_m:
            f1h = float(mf_m.get("net_inflow_1h_usd", 0.0)) / 1e6
            f15m = float(mf_m.get("net_inflow_15m_usd", 0.0)) / 1e6
            oi_m = float(mf_m.get("total_open_interest_usd", 0.0)) / 1e6
            f_icon = "🟢 ورود پول" if f1h >= 0 else "🔴 خروج پول"
            lines.extend(
                [
                    "",
                    "💵 <b>برآورد فشار خرید/فروش از OHLCV (Money Flow Proxy):</b>",
                    f"• <b>خالص برآوردی ۱ ساعته:</b> <code>{f1h:+,.2f}M$</code> ({f_icon}) | <b>آخرین کندل بسته ۱۵ دقیقه‌ای:</b> <code>{f15m:+,.2f}M$</code>",
                    f"• <b>شاخص MFI-14:</b> <code>{mf_m.get('mfi_14', 50)}</code> | <b>روند OBV:</b> <code>{'صعودی' if mf_m.get('obv_trend')=='BULLISH' else 'نزولی'}</code>",
                    f"• <b>Open Interest کل:</b> <code>${oi_m:,.1f}M</code> | <b>فاندینگ ریت:</b> <code>{mf_m.get('funding_8h_pct', 0):+.4f}%</code>",
                ]
            )

        if ind_m:
            lines.extend(
                [
                    "",
                    "📊 <b>اندیکاتورهای کلیدی (RSI ، MA و MACD):</b>",
                    f"• <b>RSI (15m / 1h / 4h):</b> <code>{ind_m.get('rsi_15m', '--')} / {ind_m.get('rsi_14', '--')} / {ind_m.get('rsi_4h', '--')}</code>",
                    f"• <b>میانگین‌های متحرک (MA):</b> <code>MA20=${_fmt_price(ind_m.get('ma20', 0))}</code> | <code>MA50=${_fmt_price(ind_m.get('ma50', 0))}</code> | <code>MA200=${_fmt_price(ind_m.get('ma200', 0))}</code>",
                    f"• <b>وضعیت MA و مکدی:</b> {ind_m.get('ma_status_fa', '—')} | MACD: {ind_m.get('macd_status_fa', '—')}",
                ]
            )

        # S/R Zones & Diagonal Trendlines Section
        srt = sig.get("sr_zones_and_trendlines") or {}
        s1_z = srt.get("s1_zone")
        r1_z = srt.get("r1_zone")
        asc_tl = srt.get("ascending_tl")
        desc_tl = srt.get("descending_tl")
        fib = srt.get("fibonacci") or {}
        if s1_z or r1_z or asc_tl or desc_tl or fib:
            lines.extend(["", "📐 <b>محدوده‌های حمایت/مقاومت و خطوط روند:</b>"])
            if s1_z:
                lines.append(
                    f"🟢 <b>محدوده حمایت اول (S1):</b> <code>${_fmt_price(s1_z['low'])} – ${_fmt_price(s1_z['high'])}</code> ({s1_z.get('touches', 2)} برخورد)"
                )
            if r1_z:
                lines.append(
                    f"🔴 <b>محدوده مقاومت اول (R1):</b> <code>${_fmt_price(r1_z['low'])} – ${_fmt_price(r1_z['high'])}</code> ({r1_z.get('touches', 2)} برخورد)"
                )
            if desc_tl:
                lines.append(
                    f"📉 <b>خط روند نزولی:</b> تراز <code>${_fmt_price(desc_tl['current_price_on_line'])}</code> — {desc_tl['status_fa']}"
                )
            if asc_tl:
                lines.append(
                    f"📈 <b>خط روند صعودی:</b> تراز <code>${_fmt_price(asc_tl['current_price_on_line'])}</code> — {asc_tl['status_fa']}"
                )
            if fib:
                impulse_fa = "صعودی" if fib.get("impulse_direction") == "BULLISH" else "نزولی"
                conf_map = {
                    "BULLISH_CONFIRMED": "همگرایی صعودی تاییدشده ✅",
                    "BEARISH_CONFIRMED": "همگرایی نزولی تاییدشده ✅",
                    "TOUCH_UNCONFIRMED": "صرفاً تماس؛ بدون تایید ورود ⏳",
                    "NONE": "دور از سطح کلیدی",
                }
                lines.extend(
                    [
                        f"🌀 <b>فیبوناچی موج {impulse_fa}:</b> نزدیک‌ترین سطح <code>{fib.get('nearest_level', '—')}</code> در <code>${_fmt_price(float(fib.get('nearest_price', 0)))}</code>",
                        f"✨ <b>ناحیه طلایی 61.8–65%:</b> <code>${_fmt_price(float(fib.get('golden_pocket_low', 0)))}</code> تا <code>${_fmt_price(float(fib.get('golden_pocket_high', 0)))}</code> | {conf_map.get(fib.get('confluence'), 'بدون تایید')}",
                        f"🎯 <b>اکستنشن‌های موج:</b> <code>1.272=${_fmt_price(float(fib.get('extension_1.272', 0)))}</code> | <code>1.618=${_fmt_price(float(fib.get('extension_1.618', 0)))}</code>",
                    ]
                )

        # Stateful Comparison vs Previous Analysis Section
        comp = sig.get("previous_comparison") or {}
        if comp.get("has_previous"):
            lines.extend(
                [
                    "",
                    "🔄 <b>مقایسه با تحلیل قبلی همین ارز:</b>",
                    f"• <b>تغییر ساختار S/R:</b> {comp.get('sr_transition_fa', '—')}",
                    f"• <b>تغییر قیمت و امتیاز کل:</b> قیمت <code>{comp.get('price_change_pct', 0):+.2f}%</code> | برآیند امتیاز <code>{comp.get('previous_score', 0):+.1f} ⬅️ {sig['composite_score']:+.1f}</code>",
                    f"• <b>سرنوشت سیگنال قبلی:</b> {comp.get('prev_signal_status_fa', '—')}",
                ]
            )
        elif comp.get("baseline_created"):
            lines.extend(["", "🔄 <b>حافظه مقایسه زمانی:</b> نقطه مرجع اولیه ثبت شد (از تحلیل بعدی تغییرات S/R و خط روند مقایسه می‌شود)."])

        lines.extend(["", "🧩 <b>گزارش تفکیکی ایجنت‌های تخصصی:</b>"])

        icon_dir = {"LONG": "🟢", "SHORT": "🔴", "NEUTRAL": "⚪️"}
        for rep in sig.get("agent_reports", []):
            ic = icon_dir.get(rep["direction"], "⚪️")
            lines.append(
                f"{ic} <b>{rep['agent_name_fa']}:</b> <code>{rep['score']:+.0f}</code> — {rep['summary_fa']}"
            )

        lines.extend(
            [
                "",
                f"📝 <b>جمع‌بندی سرتیم تحلیل:</b>\n{sig['executive_summary_fa']}",
                f"⚠️ <b>شرط ابطال:</b> {sig['invalidation_note_fa']}",
                "━━━━━━━━━━━━━━━━━━━━",
                f"🕒 <code>{sig['timestamp_iso']}</code>",
            ]
        )
        return "\n".join(lines)

    def format_rotation_message(
        self,
        old_label: str,
        old_addr: str,
        losses: int,
        new_label: Optional[str],
        new_addr: Optional[str],
        new_roi: float = 0.0,
        new_winrate: float = 0.0,
        reason_fa: str = "",
    ) -> str:
        lines = [
            "🔄🚨 <b>هشدار مدیریت ریسک کپی‌تریدر: تعویض خودکار تریدر مرجع!</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"❌ <b>تریدر قبلی:</b> <code>{old_label}</code> (<code>{old_addr[:8]}...{old_addr[-4:]}</code>)",
            f"📉 <b>علت حذف از کپی فعال:</b> ثبت <b>{losses} اشتباه متوالی</b> ({reason_fa})",
            "",
        ]
        if new_label and new_addr:
            lines.extend(
                [
                    f"✅ <b>تریدر جایگزین فعال‌شده:</b> <code>{new_label}</code>",
                    f"🔗 <b>آدرس کیف‌پول:</b> <code>{new_addr[:10]}...{new_addr[-6:]}</code>",
                    f"📊 <b>بازدهی ماهانه:</b> <code>+{new_roi:.1f}%</code> | <b>وین‌ریت:</b> <code>{new_winrate:.1f}%</code>",
                    "🛡 <b>شمارنده خطای متوالی جدید:</b> <code>0 / 2</code>",
                ]
            )
        else:
            lines.append("⚠️ تریدر جایگزین واجد شرایط در صف انتظار یافت نشد؛ در حال اسکن مجدد لیدربورد...")

        lines.extend(["━━━━━━━━━━━━━━━━━━━━", f"🕒 <code>{_now_iso()}</code>"])
        return "\n".join(lines)

    async def send_html_message(
        self,
        html_text: str,
        msg_type: str = "SIGNAL",
        symbol: Optional[str] = None,
        override_token: Optional[str] = None,
        override_chat_id: Optional[str] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        token = (override_token or settings.get("TELEGRAM_BOT_TOKEN") or "").strip()
        raw_chats = (override_chat_id or settings.get("TELEGRAM_CHAT_ID") or "").strip()
        chat_ids = [c.strip() for c in raw_chats.split(",") if c.strip()]
        ts = _now_iso()

        if not token or not chat_ids:
            note = "ذخیره در پیش‌نمایش داخلی (توکن ربات یا آیدی کانال تلگرام هنوز در تنظیمات وارد نشده است)"
            db.log_telegram(
                timestamp_iso=ts,
                msg_type=msg_type,
                symbol=symbol,
                chat_id=raw_chats or "NOT_SET",
                delivered=False,
                delivery_note=note,
                message_text=html_text,
            )
            return {"ok": False, "queued_preview": True, "note": note, "message_text": html_text}

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        delivered_any = False
        last_msg_id = None
        notes = []

        async with httpx.AsyncClient(timeout=15.0) as client:
            for cid in chat_ids:
                sent_this_chat = False
                last_err = ""
                for attempt in range(3):
                    payload: Dict[str, Any] = {
                        "chat_id": cid,
                        "text": html_text if attempt < 2 else re.sub(r"<[^>]+>", "", html_text),
                        "disable_web_page_preview": True,
                    }
                    if attempt < 2:
                        payload["parse_mode"] = "HTML"
                    if reply_markup:
                        payload["reply_markup"] = reply_markup
                    try:
                        resp = await client.post(url, json=payload)
                        data = resp.json()
                        if resp.status_code == 200 and data.get("ok"):
                            delivered_any = True
                            sent_this_chat = True
                            last_msg_id = data["result"]["message_id"]
                            notes.append(f"ارسال موفق به {cid}")
                            break
                        elif resp.status_code == 429:
                            retry_after = float(data.get("parameters", {}).get("retry_after", 2.0))
                            await asyncio.sleep(min(retry_after + 0.5, 8.0))
                            last_err = data.get("description", "HTTP 429")
                        else:
                            last_err = data.get("description", f"HTTP {resp.status_code}")
                            await asyncio.sleep(1.2 * (attempt + 1))
                    except Exception as exc:
                        last_err = str(exc)
                        await asyncio.sleep(1.5 * (attempt + 1))
                if not sent_this_chat:
                    notes.append(f"خطا برای {cid}: {last_err}")

        note_str = " | ".join(notes)
        db.log_telegram(
            timestamp_iso=ts,
            msg_type=msg_type,
            symbol=symbol,
            chat_id=",".join(chat_ids),
            delivered=delivered_any,
            delivery_note=note_str,
            message_text=html_text,
        )
        return {
            "ok": delivered_any,
            "note": note_str,
            "telegram_message_id": last_msg_id,
        }

    async def send_signal(self, signal_dict: Dict[str, Any]) -> Dict[str, Any]:
        msg = self.format_signal_message(signal_dict)
        msg_type = "COPY_SIGNAL" if signal_dict.get("signal_source") == "COPY_TRADER" else "SIGNAL"
        res = await self.send_html_message(
            msg,
            msg_type=msg_type,
            symbol=signal_dict.get("symbol"),
            reply_markup=DEFAULT_SIGNAL_INLINE_KEYBOARD,
        )
        if res.get("ok"):
            db.mark_signal_telegram_sent(signal_dict["signal_id"])
        return res


telegram_agent = TelegramNotifierAgent()
