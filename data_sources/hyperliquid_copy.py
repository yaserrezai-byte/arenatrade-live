import asyncio
import time
from typing import Any, Dict, List, Tuple
import httpx

from core.config import settings


class HyperliquidCopySource:
    """
    Connects directly to Hyperliquid Mainnet Leaderboard (48,000+ traders)
    and inspects real-time futures positions (`clearinghouseState`) and trade fills (`userFills`).
    """

    LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
    INFO_URL = "https://api.hyperliquid.xyz/info"

    def __init__(self) -> None:
        self._lb_cache: List[Dict[str, Any]] = []
        self._lb_ts: float = 0.0

    async def fetch_leaderboard_candidates(self, top_n: int = 16) -> List[Dict[str, Any]]:
        min_eq = float(settings.get("COPY_TRADER_MIN_EQUITY_USD", 50000.0))

        if self._lb_cache and (time.time() - self._lb_ts) < 300:
            rows = self._lb_cache
        else:
            async with httpx.AsyncClient(timeout=18.0) as client:
                resp = await client.get(self.LEADERBOARD_URL, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                rows = resp.json().get("leaderboardRows", [])
                self._lb_cache = rows
                self._lb_ts = time.time()

        scored_candidates: List[Dict[str, Any]] = []
        for r in rows[:1500]:
            try:
                eq = float(r.get("accountValue") or 0.0)
                if eq < min_eq or eq > 120_000_000:
                    continue

                perf = dict(r.get("windowPerformances", []))
                day_p = perf.get("day", {})
                week_p = perf.get("week", {})
                month_p = perf.get("month", {})

                pnl_day = float(day_p.get("pnl") or 0.0)
                pnl_week = float(week_p.get("pnl") or 0.0)
                pnl_month = float(month_p.get("pnl") or 0.0)
                roi_month = float(month_p.get("roi") or 0.0) * 100.0
                roi_week = float(week_p.get("roi") or 0.0) * 100.0
                vlm_week = float(week_p.get("vlm") or 0.0)

                # Must be actively trading & consistently profitable over week + month
                if pnl_month <= 0 or pnl_week <= 0 or roi_month < 4.0 or vlm_week < min_eq * 2:
                    continue
                # Avoid extreme over-leveraged gamblers (ROI > 900% in 1 month often blows up)
                if roi_month > 800.0:
                    continue

                smart_score = (
                    min(roi_month, 120.0) * 0.45
                    + min(roi_week, 50.0) * 0.35
                    + (15.0 if pnl_day >= 0 else 0.0)
                    + min(eq / 500_000.0, 20.0)
                )

                addr = r["ethAddress"]
                display_name = r.get("displayName") or f"Whale-{addr[2:6].upper()}..{addr[-4:].upper()}"
                scored_candidates.append(
                    {
                        "address": addr,
                        "label": display_name,
                        "account_value_usd": round(eq, 2),
                        "pnl_day_usd": round(pnl_day, 2),
                        "pnl_week_usd": round(pnl_week, 2),
                        "pnl_month_usd": round(pnl_month, 2),
                        "roi_month_pct": round(roi_month, 2),
                        "smart_score": round(smart_score, 2),
                    }
                )
            except Exception:
                continue

        scored_candidates.sort(key=lambda x: x["smart_score"], reverse=True)
        selected = scored_candidates[:top_n]

        # Enrich top candidates concurrently with real open positions & closed fill win-rate
        sem = asyncio.Semaphore(5)

        async def _enrich(idx: int, item: Dict[str, Any]) -> Dict[str, Any]:
            async with sem:
                positions, closed_trades, win_rate, latest_tid = await self.fetch_trader_state_and_fills(
                    item["address"]
                )
                item["rank"] = idx + 1
                item["open_positions"] = positions
                item["recent_closed_trades"] = closed_trades[:12]
                item["win_rate_pct"] = win_rate
                item["last_fill_tid"] = latest_tid
                # Boost score if trader currently has active positions and high win rate
                item["smart_score"] = round(
                    item["smart_score"] * 0.65 + win_rate * 0.35 + (5.0 if positions else 0.0), 2
                )
                return item

        enriched = await asyncio.gather(*[_enrich(i, c) for i, c in enumerate(selected)])
        enriched.sort(key=lambda x: x["smart_score"], reverse=True)
        for idx, item in enumerate(enriched):
            item["rank"] = idx + 1
        return enriched

    async def fetch_trader_state_and_fills(
        self, address: str
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float, int]:
        """Returns (open_positions, closed_trade_events, win_rate_pct, latest_tid)."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            st_task = client.post(self.INFO_URL, json={"type": "clearinghouseState", "user": address})
            fills_task = client.post(self.INFO_URL, json={"type": "userFills", "user": address})
            st_resp, fills_resp = await asyncio.gather(st_task, fills_task, return_exceptions=True)

        open_positions: List[Dict[str, Any]] = []
        if not isinstance(st_resp, Exception) and st_resp.status_code == 200:
            st_data = st_resp.json()
            for ap in st_data.get("assetPositions", []):
                pos = ap.get("position", {})
                szi = float(pos.get("szi") or 0.0)
                if abs(szi) == 0:
                    continue
                lev_info = pos.get("leverage", {})
                lev_val = int(lev_info.get("value", 1)) if isinstance(lev_info, dict) else 1
                liq_raw = pos.get("liquidationPx")
                open_positions.append(
                    {
                        "coin": str(pos.get("coin", "")).upper(),
                        "direction": "LONG" if szi > 0 else "SHORT",
                        "size": abs(szi),
                        "entry_price": float(pos.get("entryPx") or 0.0),
                        "position_value_usd": round(float(pos.get("positionValue") or 0.0), 2),
                        "unrealized_pnl_usd": round(float(pos.get("unrealizedPnl") or 0.0), 2),
                        "roe_pct": round(float(pos.get("returnOnEquity") or 0.0) * 100.0, 2),
                        "leverage": lev_val,
                        "liquidation_price": float(liq_raw) if liq_raw else None,
                    }
                )
            open_positions.sort(key=lambda x: x["position_value_usd"], reverse=True)

        closed_events: List[Dict[str, Any]] = []
        latest_tid = 0
        if not isinstance(fills_resp, Exception) and fills_resp.status_code == 200:
            fills = fills_resp.json()
            if isinstance(fills, list):
                # Group consecutive closing fills of same coin within 60s window so partial fills count as 1 logical trade
                closing_fills = []
                for f in fills[:600]:
                    tid = int(f.get("tid") or 0)
                    if tid > latest_tid:
                        latest_tid = tid
                    closed_pnl = float(f.get("closedPnl") or 0.0)
                    direction_str = str(f.get("dir", ""))
                    if "Close" in direction_str and abs(closed_pnl) > 0.5:
                        closing_fills.append(
                            {
                                "tid": tid,
                                "time": int(f.get("time") or 0),
                                "coin": str(f.get("coin", "")).upper(),
                                "dir": direction_str,
                                "px": float(f.get("px") or 0.0),
                                "closed_pnl": closed_pnl,
                            }
                        )

                # Merge partial fills belonging to the same trade execution burst (60s)
                closing_fills.sort(key=lambda x: x["time"], reverse=True)
                for cf in closing_fills:
                    if (
                        closed_events
                        and closed_events[-1]["coin"] == cf["coin"]
                        and closed_events[-1]["dir"] == cf["dir"]
                        and abs(closed_events[-1]["time"] - cf["time"]) <= 90_000
                    ):
                        closed_events[-1]["closed_pnl"] = round(
                            closed_events[-1]["closed_pnl"] + cf["closed_pnl"], 2
                        )
                        closed_events[-1]["outcome"] = (
                            "WIN" if closed_events[-1]["closed_pnl"] > 0 else "LOSS"
                        )
                    else:
                        closed_events.append(
                            {
                                "tid": cf["tid"],
                                "time": cf["time"],
                                "coin": cf["coin"],
                                "dir": cf["dir"],
                                "px": cf["px"],
                                "closed_pnl": round(cf["closed_pnl"], 2),
                                "outcome": "WIN" if cf["closed_pnl"] > 0 else "LOSS",
                            }
                        )

        wins = sum(1 for e in closed_events if e["closed_pnl"] > 0)
        total = len(closed_events)
        win_rate = round((wins / total) * 100.0, 1) if total >= 3 else 64.0

        return open_positions, closed_events, win_rate, latest_tid


hl_copy_source = HyperliquidCopySource()
