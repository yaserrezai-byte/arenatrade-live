import asyncio
from typing import Any, Dict, List, Optional
import httpx

from agents.copy_trader_agent import copy_trader_agent
from agents.orchestrator_agent import orchestrator_agent
from agents.telegram_agent import telegram_agent
from core.config import settings
from core.database import db


PERSISTENT_REPLY_KEYBOARD = {
    "keyboard": [
        [
            {"text": "🧠 تحلیل BTC"},
            {"text": "🧠 تحلیل ETH"},
            {"text": "🧠 تحلیل SOL"},
        ],
        [
            {"text": "🐋 سیگنال مستقل کپی‌تریدر"},
            {"text": "🐋 وضعیت نهنگ فعال"},
        ],
        [
            {"text": "📡 اسکن تحلیلی واچ‌لیست"},
            {"text": "📊 تحلیل XRP"},
        ],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

MAIN_KEYBOARD = {
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
            {"text": "📡 اسکن تحلیلی کل واچ‌لیست", "callback_data": "cmd:scan"},
        ],
    ]
}


class TelegramBotListener:
    """
    Interactive Telegram Bot Command & Callback Listener + Auto-Channel/Group Detector:
    - Responds to /start, /help, /signal <COIN>, /copy, /scan, or plain coin names (e.g. BTC, SOL).
    - Automatically detects when the bot is added as Admin/Member to a Group or Channel (`my_chat_member`)
      and links that Group/Channel ID automatically!
    """

    def __init__(self) -> None:
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._offset: int = 0
        self.recent_discovered_chats: List[Dict[str, Any]] = []

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    def _register_chat_id(self, new_cid: str) -> bool:
        existing_raw = str(settings.get("TELEGRAM_CHAT_ID") or "")
        parts = [p.strip() for p in existing_raw.split(",") if p.strip()]
        if new_cid not in parts:
            parts.append(new_cid)
            settings.update({"TELEGRAM_CHAT_ID": ",".join(parts)})
            return True
        return False

    def _remember_chat(self, ch: Dict[str, Any]) -> None:
        cid = str(ch.get("id"))
        title = ch.get("title") or ch.get("username") or ch.get("first_name") or cid
        ctype = ch.get("type", "unknown")
        self.recent_discovered_chats = [c for c in self.recent_discovered_chats if c["numeric_id"] != cid]
        self.recent_discovered_chats.insert(
            0,
            {
                "chat_id": f"@{ch['username']}" if ch.get("username") and ctype == "channel" else cid,
                "numeric_id": cid,
                "title": title,
                "type": ctype,
            },
        )

    async def process_pending_updates_once(self) -> int:
        token = str(settings.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            return 0
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        handled = 0
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.get(
                    url,
                    params={
                        "timeout": 0,
                        "allowed_updates": '["message","channel_post","callback_query","my_chat_member"]',
                    },
                )
                data = resp.json()
                if not data.get("ok"):
                    return 0
                updates = data.get("result", [])
                last_offset = None
                for upd in updates[-8:]:
                    last_offset = int(upd["update_id"]) + 1
                    await self._handle_update(token, upd)
                    handled += 1
                if last_offset is not None:
                    await client.get(url, params={"offset": last_offset, "timeout": 0})
        except Exception:
            pass
        return handled

    async def _poll_loop(self) -> None:
        await asyncio.sleep(2)
        while self._running:
            token = str(settings.get("TELEGRAM_BOT_TOKEN") or "").strip()
            if not token:
                await asyncio.sleep(8)
                continue

            url = f"https://api.telegram.org/bot{token}/getUpdates"
            try:
                async with httpx.AsyncClient(timeout=28.0) as client:
                    resp = await client.get(
                        url,
                        params={
                            "offset": self._offset,
                            "timeout": 20,
                            "allowed_updates": '["message","channel_post","callback_query","my_chat_member"]',
                        },
                    )
                    data = resp.json()
                    if not data.get("ok"):
                        await asyncio.sleep(6)
                        continue

                    for upd in data.get("result", []):
                        self._offset = int(upd["update_id"]) + 1
                        asyncio.create_task(self._handle_update(token, upd))
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(5)

    async def _handle_update(self, token: str, upd: Dict[str, Any]) -> None:
        # 1. Auto-detect when bot is added to a Group or Channel!
        mcm = upd.get("my_chat_member")
        if isinstance(mcm, dict):
            chat = mcm.get("chat", {})
            new_state = mcm.get("new_chat_member", {}).get("status", "")
            ctype = chat.get("type", "")
            cid = str(chat.get("id", ""))
            title = chat.get("title") or cid
            self._remember_chat(chat)

            if ctype in ("group", "supergroup", "channel") and new_state in ("administrator", "member"):
                added = self._register_chat_id(cid)
                if added:
                    welcome_grp = (
                        f"✅ <b>اتصال خودکار برقرار شد!</b>\n"
                        f"این {('کانال' if ctype == 'channel' else 'گروه')} (<b>{title}</b> | <code>{cid}</code>) "
                        f"به صورت خودکار به سیستم چند-ایجنته سیگنال فیوچرز و کپی‌تریدر اضافه شد.\n\n"
                        f"از این لحظه سیگنال‌های تاییدشده و هشدار تعویض تریدرها اینجا ارسال می‌شوند."
                    )
                    await telegram_agent.send_html_message(
                        welcome_grp,
                        msg_type="AUTO_GROUP_LINK",
                        override_chat_id=cid,
                        reply_markup=MAIN_KEYBOARD,
                    )
            return

        # 2. Inline Keyboard button callbacks
        cb = upd.get("callback_query")
        if isinstance(cb, dict):
            cb_id = cb.get("id")
            cb_data = str(cb.get("data") or "")
            msg = cb.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))

            # Answer callback quickly so button spinner stops
            try:
                async with httpx.AsyncClient(timeout=6.0) as client:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                        json={"callback_query_id": cb_id, "text": "در حال پردازش توسط ایجنت‌ها..."},
                    )
            except Exception:
                pass

            if cb_data.startswith("sig:"):
                coin = cb_data.split(":", 1)[1]
                await self._reply_coin_analysis(chat_id, coin)
            elif cb_data == "cmd:copysig":
                await self._reply_copy_signal(chat_id)
            elif cb_data == "cmd:copy":
                await self._reply_copy_status(chat_id)
            elif cb_data == "cmd:scan":
                await self._reply_watchlist_scan(chat_id)
            return

        # 3. Text Messages / Commands in Private Chat or Group/Channel
        msg_obj = upd.get("message") or upd.get("channel_post")
        if not isinstance(msg_obj, dict):
            return

        chat = msg_obj.get("chat", {})
        self._remember_chat(chat)
        chat_id = str(chat.get("id", ""))
        text = str(msg_obj.get("text") or "").strip()
        if not text:
            return

        low = text.lower().split("@")[0]  # handle /command@Arenatrade2_Bot in groups

        if low in ("/start", "/help", "منو", "راهنما"):
            help_html = (
                "🤖 <b>ربات چند-ایجنته تحلیل فیوچرز و کپی‌تریدر هوشمند (@Arenatrade2_Bot)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "دستورات قابل استفاده در چت خصوصی یا گروه:\n"
                "🔹 <code>/signal BTC</code> یا نوشتن نام ارز (مثلاً <code>SOL</code>) — تحلیل مستقل با ۸ ایجنت تخصصی\n"
                "🔹 <code>/copysig</code> — دریافت سیگنال مستقل کپی‌تریدر از نهنگ فعال\n"
                "🔹 <code>/copy</code> — وضعیت نهنگ کپی‌شده و شمارنده ۲ خطای متوالی\n"
                "🔹 <code>/scan</code> — اسکن کل واچ‌لیست و یافتن بهترین ستاپ‌ها\n"
                "🔹 <code>/setchat</code> — ثبت این گروه/کانال به عنوان مقصد خودکار سیگنال‌ها\n\n"
                "💡 <i>از دکمه‌های شیشه‌ای زیر پیام یا کیبورد ثابت پایین صفحه استفاده کنید:</i>"
            )
            if chat.get("type") in ("private", "group", "supergroup"):
                await telegram_agent.send_html_message(
                    "⌨️ <b>کیبورد سریع پایین صفحه فعال شد.</b>",
                    msg_type="BOT_MENU",
                    override_chat_id=chat_id,
                    reply_markup=PERSISTENT_REPLY_KEYBOARD,
                )
            await telegram_agent.send_html_message(
                help_html, msg_type="BOT_MENU", override_chat_id=chat_id, reply_markup=MAIN_KEYBOARD
            )

        elif low == "/setchat":
            self._register_chat_id(chat_id)
            await telegram_agent.send_html_message(
                f"✅ آیدی <code>{chat_id}</code> در لیست مقاصد خودکار سیگنال ذخیره شد.",
                msg_type="BOT_CMD",
                override_chat_id=chat_id,
            )

        elif low == "/copy" or text == "🐋 وضعیت نهنگ فعال":
            await self._reply_copy_status(chat_id)

        elif low in ("/copysig", "/copysignal") or text == "🐋 سیگنال مستقل کپی‌تریدر":
            await self._reply_copy_signal(chat_id)

        elif low == "/scan" or text == "📡 اسکن تحلیلی واچ‌لیست":
            await self._reply_watchlist_scan(chat_id)

        elif text in ("🧠 تحلیل BTC", "🧠 تحلیل ETH", "🧠 تحلیل SOL", "📊 تحلیل XRP"):
            coin = text.split()[-1].upper()
            await self._reply_coin_analysis(chat_id, coin)

        elif low.startswith("/signal"):
            parts = text.split()
            coin = parts[1].upper() if len(parts) > 1 else "BTC"
            await self._reply_coin_analysis(chat_id, coin)

        elif len(text) <= 8 and text.isalpha() and chat.get("type") == "private":
            # Direct symbol typing in private chat, e.g. "BTC", "ETH", "DOGE"
            await self._reply_coin_analysis(chat_id, text.upper())

    async def _reply_coin_analysis(self, chat_id: str, coin: str) -> None:
        await telegram_agent.send_html_message(
            f"⏳ در حال اجرای همزمان ۸ ایجنت تخصصی روی <b>#{coin}USDT</b>...",
            msg_type="BOT_WAIT",
            override_chat_id=chat_id,
        )
        res = await orchestrator_agent.analyze_symbol(coin, send_to_telegram=False)
        msg = telegram_agent.format_signal_message(res["signal"])
        await telegram_agent.send_html_message(
            msg,
            msg_type="SIGNAL",
            symbol=coin,
            override_chat_id=chat_id,
            reply_markup=MAIN_KEYBOARD,
        )

    async def _reply_copy_status(self, chat_id: str) -> None:
        await copy_trader_agent.monitor_active_trader()
        active = db.get_active_trader()
        if not active:
            await telegram_agent.send_html_message(
                "⚠️ در حال بارگذاری لیست تریدرهای برتر...", override_chat_id=chat_id
            )
            return
        pos_lines = []
        for p in (active.get("open_positions") or [])[:6]:
            ic = "🟢" if p["direction"] == "LONG" else "🔴"
            pos_lines.append(
                f"{ic} <b>{p['coin']}</b> ({p['direction']} {p['leverage']}x) | ورود: <code>${p['entry_price']:,.4g}</code> | حجم: <code>${p['position_value_usd']:,.0f}</code>"
            )
        pos_txt = "\n".join(pos_lines) if pos_lines else "فعلاً پوزیشن بازی ندارد."
        html = (
            "🐋 <b>وضعیت لحظه‌ای ایجنت کپی‌تریدر هوشمند</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>تریدر فعال:</b> <code>{active['label']}</code>\n"
            f"💰 <b>موجودی حساب:</b> <code>${active['account_value_usd']:,.0f}</code>\n"
            f"📈 <b>بازدهی ۳۰ روزه:</b> <code>+{active['roi_month_pct']}%</code> (<code>+${active['pnl_month_usd']:,.0f}</code>)\n"
            f"🎯 <b>نرخ برد واقعی:</b> <code>{active['win_rate_pct']}%</code>\n"
            f"🛡 <b>شمارنده خطای متوالی:</b> <code>{active['consecutive_losses']} / 2</code>\n\n"
            f"📌 <b>پوزیشن‌های باز فعلی نهنگ:</b>\n{pos_txt}"
        )
        await telegram_agent.send_html_message(
            html, msg_type="COPY_STATUS", override_chat_id=chat_id, reply_markup=MAIN_KEYBOARD
        )

    async def _reply_copy_signal(self, chat_id: str) -> None:
        res = await copy_trader_agent.emit_copy_signals_now(send_to_telegram=False)
        if not res.get("ok"):
            await telegram_agent.send_html_message(
                f"🐋 {res.get('error', 'پوزیشن بازی یافت نشد.')}",
                msg_type="COPY_STATUS",
                override_chat_id=chat_id,
                reply_markup=MAIN_KEYBOARD,
            )
            return
        for sig in res.get("signals", []):
            msg = telegram_agent.format_copy_trade_message(sig)
            await telegram_agent.send_html_message(
                msg,
                msg_type="COPY_SIGNAL",
                symbol=sig["symbol"],
                override_chat_id=chat_id,
                reply_markup=MAIN_KEYBOARD,
            )

    async def _reply_watchlist_scan(self, chat_id: str) -> None:
        from services.scanner_service import scanner_service

        await telegram_agent.send_html_message(
            "📡 در حال اسکن کامل واچ‌لیست با ۸ ایجنت تخصصی... لطفاً چند ثانیه صبر کنید.",
            msg_type="BOT_WAIT",
            override_chat_id=chat_id,
        )
        results = await scanner_service.scan_watchlist_once(send_summary_to_telegram=False)
        lines = ["📡 <b>نتیجه اسکن همزمان واچ‌لیست فیوچرز:</b>", "━━━━━━━━━━━━━━━━━━━━"]
        for r in results[:8]:
            s = r["signal"]
            ic = "🟢" if s["direction"] == "LONG" else ("🔴" if s["direction"] == "SHORT" else "⚪️")
            act = "✅ تاییدشده" if s["actionable"] else "⏳ تحت نظر"
            lines.append(
                f"{ic} <b>#{s['symbol']}</b>: <code>{s['direction']}</code> | اطمینان: <code>{s['confidence']}%</code> | R:R <code>1:{s['risk_reward_tp2']}</code> ({act})"
            )
        await telegram_agent.send_html_message(
            "\n".join(lines), msg_type="SCAN_SUMMARY", override_chat_id=chat_id, reply_markup=MAIN_KEYBOARD
        )


telegram_bot_listener = TelegramBotListener()
