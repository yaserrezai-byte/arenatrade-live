import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Dict
from aiohttp import web

from agents.copy_trader_agent import copy_trader_agent
from agents.orchestrator_agent import orchestrator_agent
from agents.telegram_agent import telegram_agent
from core.config import settings
from core.database import db
from core.llm_router import llm_router
from data_sources.market_data import market_hub
from services.scanner_service import scanner_service
from services.signal_validation_service import signal_validation_service
from services.telegram_bot_listener import telegram_bot_listener, MAIN_KEYBOARD

WEB_DIR = Path(__file__).resolve().parent / "web"


@web.middleware
async def cors_and_preview_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as ex:
            resp = ex
        except Exception as exc:
            resp = web.json_response({"ok": False, "error": str(exc)}, status=500)

    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    # Ensure iframe live preview works smoothly
    if "X-Frame-Options" in resp.headers:
        del resp.headers["X-Frame-Options"]
    return resp


async def api_status(request: web.Request) -> web.Response:
    hl_ctxs = {}
    try:
        hl_ctxs = await market_hub.get_hl_all_contexts()
    except Exception:
        pass

    tickers = []
    for sym in settings.watchlist:
        c = hl_ctxs.get(sym, {})
        tickers.append(
            {
                "symbol": sym,
                "mark_price": c.get("mark_price", 0.0),
                "change_24h_pct": round(float(c.get("change_24h_pct", 0.0)), 2),
                "funding_8h_pct": round(float(c.get("funding_8h_equiv", 0.0)) * 100.0, 4),
                "open_interest_usd": round(float(c.get("open_interest_usd", 0.0)), 0),
            }
        )

    active_trader = db.get_active_trader()
    return web.json_response(
        {
            "ok": True,
            "ai_engine": llm_router.active_provider_info(),
            "telegram_configured": bool(
                settings.get("TELEGRAM_BOT_TOKEN") and settings.get("TELEGRAM_CHAT_ID")
            ),
            "watchlist": settings.watchlist,
            "primary_timeframe": settings.get("PRIMARY_TIMEFRAME", "1h"),
            "tickers": tickers,
            "active_trader": active_trader,
            "scanner": {
                "auto_enabled": settings.get("AUTO_SCAN_ENABLED", True),
                "is_scanning": scanner_service.is_scanning_now,
                "last_scan_ts": scanner_service.last_scan_ts,
            },
        }
    )


async def api_analyze(request: web.Request) -> web.Response:
    symbol = request.query.get("symbol", "BTC").upper()
    timeframe = request.query.get("timeframe") or str(settings.get("PRIMARY_TIMEFRAME", "1h"))
    send_tg_param = request.query.get("send_telegram")
    send_tg = None
    if send_tg_param is not None:
        send_tg = send_tg_param.lower() in ("1", "true", "yes")

    result = await orchestrator_agent.analyze_symbol(
        symbol=symbol, timeframe=timeframe, send_to_telegram=send_tg
    )
    return web.json_response({"ok": True, **result})


async def api_scan_watchlist(request: web.Request) -> web.Response:
    body: Dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}
    symbols = body.get("symbols") or settings.watchlist
    results = await scanner_service.scan_watchlist_once(symbols=symbols)
    return web.json_response(
        {
            "ok": True,
            "count": len(results),
            "signals": [r["signal"] for r in results],
        }
    )


async def api_signals(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", "25"))
    actionable_only = request.query.get("actionable_only", "false").lower() == "true"
    signal_source = request.query.get("source") or None
    signals = db.list_signals(limit=limit, actionable_only=actionable_only, signal_source=signal_source)
    return web.json_response({"ok": True, "signals": signals})


async def api_copy_send_signal(request: web.Request) -> web.Response:
    body: Dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}
    coin = body.get("coin")
    res = await copy_trader_agent.emit_copy_signals_now(coin=coin, send_to_telegram=True)
    return web.json_response(res)


async def api_copy_traders(request: web.Request) -> web.Response:
    traders = db.get_traders()
    if not traders:
        traders = await copy_trader_agent.refresh_leaderboard_roster(force_reseed=True)
    rotations = db.list_rotations(limit=20)
    max_losses = int(settings.get("COPY_TRADER_MAX_CONSECUTIVE_LOSSES", 2))
    return web.json_response(
        {
            "ok": True,
            "max_consecutive_losses": max_losses,
            "active_trader": next((t for t in traders if t["status"] == "ACTIVE"), None),
            "traders": traders,
            "rotations": rotations,
        }
    )


async def api_copy_refresh(request: web.Request) -> web.Response:
    body: Dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}
    reseed = bool(body.get("force_reseed", False))
    traders = await copy_trader_agent.refresh_leaderboard_roster(force_reseed=reseed)
    return web.json_response(
        {
            "ok": True,
            "active_trader": next((t for t in traders if t["status"] == "ACTIVE"), None),
            "traders": traders,
            "rotations": db.list_rotations(limit=20),
        }
    )


async def api_copy_outcome(request: web.Request) -> web.Response:
    body = await request.json()
    outcome = str(body.get("outcome", "LOSS")).upper()
    note = str(body.get("note", ""))
    res = await copy_trader_agent.record_trade_outcome(outcome=outcome, note_fa=note)
    res["traders"] = db.get_traders()
    res["rotations"] = db.list_rotations(limit=20)
    return web.json_response(res)


async def api_copy_activate(request: web.Request) -> web.Response:
    body = await request.json()
    address = str(body.get("address", "")).strip()
    if not address:
        return web.json_response({"ok": False, "error": "آدرس کیف‌پول الزامی است."}, status=400)
    active = await copy_trader_agent.activate_specific_trader(address)
    return web.json_response(
        {
            "ok": True,
            "active_trader": active,
            "traders": db.get_traders(),
        }
    )


async def api_telegram_send_signal(request: web.Request) -> web.Response:
    body = await request.json()
    signal_id = body.get("signal_id")
    signal_obj = body.get("signal")

    if not signal_obj and signal_id:
        for s in db.list_signals(limit=50):
            if s["signal_id"] == signal_id:
                signal_obj = s
                break

    if not signal_obj:
        return web.json_response({"ok": False, "error": "سیگنال مورد نظر یافت نشد."}, status=404)

    res = await telegram_agent.send_signal(signal_obj)
    return web.json_response(res)


async def api_telegram_test(request: web.Request) -> web.Response:
    body: Dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}
    token = body.get("bot_token")
    chat_id = body.get("chat_id")
    msg = (
        "✅ <b>تست اتصال سیستم چند-ایجنته فیوچرز و کپی‌تریدر</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "ربات تحلیلگر بازار ارز دیجیتال با موفقیت به این کانال تلگرام متصل شد.\n"
        "• ایجنت‌های فعال: <code>قیمت، جریان پول، اندیکاتورها، روند، حمایت/مقاومت، فاندامنتال، اخبار، کپی‌تریدر</code>\n"
        "• قانون کپی‌تریدر: <code>تعویض خودکار پس از ۲ خطای متوالی</code>"
    )
    res = await telegram_agent.send_html_message(
        msg,
        msg_type="CONNECTION_TEST",
        override_token=token if token and not str(token).startswith("***") else None,
        override_chat_id=chat_id,
    )
    return web.json_response(res)


async def api_telegram_detect_chats(request: web.Request) -> web.Response:
    body: Dict[str, Any] = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}
    raw_tok = str(body.get("bot_token") or "").strip()
    token = raw_tok if (raw_tok and not raw_tok.startswith("***")) else str(settings.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return web.json_response({"ok": False, "error": "ابتدا توکن ربات (TELEGRAM_BOT_TOKEN) را وارد کنید."}, status=400)

    # First return chats captured by live listener if running on the same token
    if telegram_bot_listener.recent_discovered_chats:
        return web.json_response({"ok": True, "chats": telegram_bot_listener.recent_discovered_chats})

    import httpx
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params={"timeout": 1})
            data = resp.json()
            if not data.get("ok"):
                return web.json_response({"ok": False, "error": data.get("description", "توکن نامعتبر است.")})

            chats: Dict[str, Dict[str, Any]] = {}
            for upd in data.get("result", []):
                for key in ("message", "channel_post", "edited_channel_post", "my_chat_member", "chat_member"):
                    obj = upd.get(key)
                    if isinstance(obj, dict) and isinstance(obj.get("chat"), dict):
                        ch = obj["chat"]
                        cid = str(ch.get("id"))
                        title = ch.get("title") or ch.get("username") or ch.get("first_name") or cid
                        ctype = ch.get("type", "unknown")
                        chats[cid] = {
                            "chat_id": f"@{ch['username']}" if ch.get("username") and ctype == "channel" else cid,
                            "numeric_id": cid,
                            "title": title,
                            "type": ctype,
                        }
            return web.json_response({"ok": True, "chats": list(chats.values())})
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"خطا در ارتباط با تلگرام: {exc}"})


async def api_telegram_logs(request: web.Request) -> web.Response:
    logs = db.list_telegram_logs(limit=25)
    return web.json_response({"ok": True, "logs": logs})


async def api_get_settings(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "settings": settings.public_dict()})


async def api_update_settings(request: web.Request) -> web.Response:
    body = await request.json()
    updated = settings.update(body)
    return web.json_response({"ok": True, "settings": updated})


async def index_handler(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


async def on_startup(app: web.Application) -> None:
    asyncio.create_task( _init_and_start() )


async def _init_and_start() -> None:
    await scanner_service.initialize()
    scanner_service.start_background_loops()
    telegram_bot_listener.start()
    signal_validation_service.start()


async def on_cleanup(app: web.Application) -> None:
    await signal_validation_service.stop()
    await telegram_bot_listener.stop()
    await scanner_service.stop_background_loops()


def create_app() -> web.Application:
    app = web.Application(middlewares=[cors_and_preview_middleware])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/", index_handler)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/analyze", api_analyze)
    app.router.add_post("/api/scan-watchlist", api_scan_watchlist)
    app.router.add_get("/api/signals", api_signals)
    app.router.add_get("/api/copy-traders", api_copy_traders)
    app.router.add_post("/api/copy-traders/refresh", api_copy_refresh)
    app.router.add_post("/api/copy-traders/outcome", api_copy_outcome)
    app.router.add_post("/api/copy-traders/activate", api_copy_activate)
    app.router.add_post("/api/copy-traders/send-signal", api_copy_send_signal)
    app.router.add_post("/api/telegram/send-signal", api_telegram_send_signal)
    app.router.add_post("/api/telegram/test", api_telegram_test)
    app.router.add_post("/api/telegram/detect-chats", api_telegram_detect_chats)
    app.router.add_get("/api/telegram/logs", api_telegram_logs)
    app.router.add_get("/api/settings", api_get_settings)
    app.router.add_post("/api/settings", api_update_settings)
    app.router.add_static("/static", WEB_DIR, show_index=False)
    return app


async def run_cli(symbol: str | None, scan_once: bool) -> None:
    await scanner_service.initialize()
    if scan_once:
        results = await scanner_service.scan_watchlist_once()
        for r in results:
            s = r["signal"]
            print(
                f"[{s['symbol']}] {s['direction']} | Conf: {s['confidence']}% | "
                f"Score: {s['composite_score']:+.1f} | Entry: {s['entry_low']:.4g}-{s['entry_high']:.4g} | "
                f"SL: {s['stop_loss']:.4g} | TP2: {s['tp2']:.4g} | Actionable: {s['actionable']}"
            )
    elif symbol:
        res = await orchestrator_agent.analyze_symbol(symbol)
        print(json.dumps(res["signal"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-Agent Crypto Futures Signal & Smart Copy-Trader")
    parser.add_argument("--cli", type=str, help="Analyze a single symbol in CLI mode (e.g. BTC)")
    parser.add_argument("--scan-once", action="store_true", help="Scan entire watchlist once and exit")
    args = parser.parse_args()

    if args.cli or args.scan_once:
        asyncio.run(run_cli(args.cli, args.scan_once))
    else:
        host = str(settings.get("HOST", "0.0.0.0"))
        port = int(settings.get("PORT", 8000))
        web.run_app(create_app(), host=host, port=port, print=lambda msg: print(msg, flush=True))
