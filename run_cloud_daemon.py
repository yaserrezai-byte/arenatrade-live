"""
Continuous 24/7 Cloud Cycle Runner for GitHub Actions (bypassing GitHub 15m cron lag).
Runs continuously for DAEMON_DURATION_SEC (default 9900s = 2h45m) per cycle:
  1) Exact 15-minute candle-synced 8-Agent Market Analysis Watchlist Scans (:00, :15, :30, :45 + 5s)
  2) 90-second live Top-3 Elite Whale Copy-Trader Radar (new signals, exit alerts, 2-strike rotation)
  3) Real-time interactive Telegram Bot buttons & commands (<2s response)
  4) Independent Watchdog & Delivery Auditor Agent (60s SLA audit + auto-heal + retry delivery)
Then saves SQLite state cleanly and hands off to the next cycle with zero downtime!
"""
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import time

from agents.watchdog_agent import watchdog_agent
from core.config import settings
from services.scanner_service import scanner_service
from services.signal_validation_service import signal_validation_service
from services.telegram_bot_listener import telegram_bot_listener


async def main() -> None:
    Path("data").mkdir(parents=True, exist_ok=True)
    settings.reload()
    duration_sec = int(os.environ.get("DAEMON_DURATION_SEC", "9900"))
    start_ts = time.time()
    end_ts = start_ts + duration_sec

    print(
        f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] "
        f"Starting Arenatrade 24/7 Cloud Cycle + Watchdog Supervisor for {duration_sec // 60}m...",
        flush=True,
    )

    await scanner_service.initialize()
    scanner_service.start_background_loops()
    telegram_bot_listener.start()
    signal_validation_service.start()
    watchdog_agent.start_supervisor()

    try:
        while time.time() < end_ts:
            await asyncio.sleep(min(30.0, max(1.0, end_ts - time.time())))
            elapsed_m = int((time.time() - start_ts) // 60)
            if elapsed_m > 0 and int(time.time() - start_ts) % 900 < 30:
                print(
                    f"[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] "
                    f"Cloud daemon & Watchdog healthy (uptime={elapsed_m}m, watchdog_checks={watchdog_agent.checks_performed})",
                    flush=True,
                )
    finally:
        print(
            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] "
            "Gracefully stopping loops for atomic SQLite state handoff...",
            flush=True,
        )
        await watchdog_agent.stop_supervisor()
        await signal_validation_service.stop()
        await telegram_bot_listener.stop()
        await scanner_service.stop_background_loops()
        await asyncio.sleep(1.0)
        print("Cycle completed cleanly.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
