"""
One-shot 15-minute execution script for GitHub Actions Cloud Cron / Linux Cron.
Runs BOTH strictly separated signal engines + processes any queued Telegram button commands:
  1) Pending Telegram Button/Command Queue (if user tapped buttons while serverless)
  2) Pure 8-Agent Market Analysis Watchlist Scan -> standalone Market Analysis signals + hourly/event summary
  3) Independent Hyperliquid Smart Copy-Trader Monitor (Top-3 Elite Whales) -> 2-strike auto-rotation + Exit alerts + standalone Copy signals
"""
import asyncio
from pathlib import Path

from agents.copy_trader_agent import copy_trader_agent
from core.config import settings
from services.scanner_service import scanner_service
from services.telegram_bot_listener import telegram_bot_listener


async def main() -> None:
    Path("data").mkdir(parents=True, exist_ok=True)
    settings.reload()
    await scanner_service.initialize()

    # 1) Process any queued Telegram commands/callbacks
    handled = await telegram_bot_listener.process_pending_updates_once()
    print(f"[TelegramQueue] Processed {handled} pending updates.")

    # 2) Independent Market Analysis 15-minute scan
    results = await scanner_service.scan_watchlist_once(
        symbols=settings.watchlist,
        send_summary_to_telegram=True,
    )
    sent_ma = [r["signal"]["symbol"] for r in results if r["signal"].get("telegram_sent")]
    print(f"[MarketAnalysis] Scanned {len(results)} coins | Telegram signals sent: {sent_ma}")

    # 3) Independent Smart Copy-Trader check & 2-strike rotation (Top-3 Elite Whales)
    copy_res = await copy_trader_agent.monitor_active_trader(auto_emit_new_signals=True)
    emitted_copy = [s["symbol"] for s in copy_res.get("emitted_signals", [])]
    print(
        f"[CopyTrader] Monitor completed: {copy_res.get('status', 'ok')} | "
        f"New Copy-Trader signals sent to Telegram: {emitted_copy}"
    )


if __name__ == "__main__":
    asyncio.run(main())
