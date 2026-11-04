import asyncio
import time
from typing import Any, Dict, List, Optional
import httpx
import pandas as pd


class MarketDataHub:
    """
    Aggregates real-time Futures OHLCV, Open Interest, Funding Rates, Basis Premium,
    and L2 Orderbook Liquidity across Hyperliquid Perpetuals + OKX Swap + Bitget USDT-Futures
    + Binance Data Vision fallback.
    """

    HL_INFO_URL = "https://api.hyperliquid.xyz/info"
    OKX_BASE = "https://www.okx.com"
    BITGET_BASE = "https://api.bitget.com"
    BINANCE_VISION = "https://data-api.binance.vision"

    INTERVAL_MAP_OKX = {
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "1H",
        "4h": "4H",
        "1d": "1Dutc",
    }

    INTERVAL_MS = {
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "4h": 14_400_000,
        "1d": 86_400_000,
    }

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}
        self._cache_ts: Dict[str, float] = {}

    def _get_cached(self, key: str, ttl: float) -> Optional[Any]:
        if key in self._cache and (time.time() - self._cache_ts.get(key, 0)) < ttl:
            return self._cache[key]
        return None

    def _set_cached(self, key: str, val: Any) -> Any:
        self._cache[key] = val
        self._cache_ts[key] = time.time()
        return val

    async def get_hl_all_contexts(self) -> Dict[str, Dict[str, Any]]:
        cached = self._get_cached("hl_meta_ctxs", 20.0)
        if cached is not None:
            return cached

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self.HL_INFO_URL, json={"type": "metaAndAssetCtxs"})
            resp.raise_for_status()
            meta, ctxs = resp.json()
            universe = meta.get("universe", [])
            mapped: Dict[str, Dict[str, Any]] = {}
            for u, c in zip(universe, ctxs):
                name = u.get("name", "").upper()
                mark_px = float(c.get("markPx") or c.get("midPx") or 0.0)
                oi_coins = float(c.get("openInterest") or 0.0)
                prev_px = float(c.get("prevDayPx") or mark_px or 1.0)
                chg_pct = ((mark_px - prev_px) / prev_px * 100.0) if prev_px > 0 else 0.0
                mapped[name] = {
                    "symbol": name,
                    "max_leverage": int(u.get("maxLeverage", 20)),
                    "mark_price": mark_px,
                    "oracle_price": float(c.get("oraclePx") or mark_px),
                    "funding_hourly": float(c.get("funding") or 0.0),
                    "funding_8h_equiv": float(c.get("funding") or 0.0) * 8.0,
                    "open_interest_coins": oi_coins,
                    "open_interest_usd": oi_coins * mark_px,
                    "volume_24h_usd": float(c.get("dayNtlVlm") or 0.0),
                    "premium": float(c.get("premium") or 0.0),
                    "change_24h_pct": chg_pct,
                }
            return self._set_cached("hl_meta_ctxs", mapped)

    async def fetch_ohlcv(self, symbol: str, interval: str = "1h", limit: int = 210) -> pd.DataFrame:
        coin = symbol.upper().replace("USDT", "").strip()
        cache_key = f"ohlcv:{coin}:{interval}:{limit}"
        cached = self._get_cached(cache_key, 20.0)
        if cached is not None:
            return cached.copy()

        # Primary 1: OKX Perpetual Swap (includes rich taker/USD quote volume)
        try:
            df = await self._fetch_okx_candles(coin, interval, limit)
            if len(df) >= 50:
                return self._set_cached(cache_key, df).copy()
        except Exception:
            pass

        # Primary 2: Hyperliquid Perpetuals candleSnapshot
        try:
            df = await self._fetch_hl_candles(coin, interval, limit)
            if len(df) >= 50:
                return self._set_cached(cache_key, df).copy()
        except Exception:
            pass

        # Fallback 3: Binance Data Vision Klines
        df = await self._fetch_binance_vision_candles(coin, interval, limit)
        return self._set_cached(cache_key, df).copy()

    async def _fetch_okx_candles(self, coin: str, interval: str, limit: int) -> pd.DataFrame:
        bar = self.INTERVAL_MAP_OKX.get(interval, "1H")
        inst_id = f"{coin}-USDT-SWAP"
        url = f"{self.OKX_BASE}/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={min(limit, 300)}"
        async with httpx.AsyncClient(timeout=9.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            rows = resp.json().get("data", [])
            if not rows:
                raise ValueError(f"Empty OKX candles for {inst_id}")
            records = []
            for r in reversed(rows):
                records.append(
                    {
                        "timestamp": int(r[0]),
                        "open": float(r[1]),
                        "high": float(r[2]),
                        "low": float(r[3]),
                        "close": float(r[4]),
                        "volume": float(r[6]),
                        "quote_volume": float(r[7]),
                    }
                )
            return pd.DataFrame(records)

    async def _fetch_hl_candles(self, coin: str, interval: str, limit: int) -> pd.DataFrame:
        step_ms = self.INTERVAL_MS.get(interval, 3_600_000)
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - step_ms * (limit + 5)
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
            },
        }
        async with httpx.AsyncClient(timeout=9.0) as client:
            resp = await client.post(self.HL_INFO_URL, json=payload)
            resp.raise_for_status()
            rows = resp.json()
            records = []
            for r in rows[-limit:]:
                c = float(r["c"])
                v = float(r["v"])
                records.append(
                    {
                        "timestamp": int(r["t"]),
                        "open": float(r["o"]),
                        "high": float(r["h"]),
                        "low": float(r["l"]),
                        "close": c,
                        "volume": v,
                        "quote_volume": v * c,
                    }
                )
            return pd.DataFrame(records)

    async def _fetch_binance_vision_candles(self, coin: str, interval: str, limit: int) -> pd.DataFrame:
        url = f"{self.BINANCE_VISION}/api/v3/klines?symbol={coin}USDT&interval={interval}&limit={min(limit, 500)}"
        async with httpx.AsyncClient(timeout=9.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            rows = resp.json()
            records = []
            for r in rows:
                records.append(
                    {
                        "timestamp": int(r[0]),
                        "open": float(r[1]),
                        "high": float(r[2]),
                        "low": float(r[3]),
                        "close": float(r[4]),
                        "volume": float(r[5]),
                        "quote_volume": float(r[7]),
                    }
                )
            return pd.DataFrame(records)

    async def fetch_derivatives_bundle(self, symbol: str) -> Dict[str, Any]:
        """
        Fetches cross-exchange Futures metrics:
        - Hyperliquid OI, Funding, Basis Premium, L2 Orderbook walls
        - Bitget USDT-Futures Funding, OI, 24h Volume
        - OKX USDT-SWAP Open Interest
        """
        coin = symbol.upper().replace("USDT", "").strip()
        cache_key = f"deriv:{coin}"
        cached = self._get_cached(cache_key, 20.0)
        if cached is not None:
            return cached

        hl_ctxs_task = self.get_hl_all_contexts()
        hl_book_task = self._fetch_hl_orderbook(coin)
        bitget_task = self._fetch_bitget_futures(coin)
        okx_oi_task = self._fetch_okx_oi(coin)

        hl_ctxs, hl_book, bitget_info, okx_oi = await asyncio.gather(
            hl_ctxs_task, hl_book_task, bitget_task, okx_oi_task, return_exceptions=True
        )

        hl_coin = hl_ctxs.get(coin, {}) if isinstance(hl_ctxs, dict) else {}
        if isinstance(hl_book, Exception):
            hl_book = {"bids": [], "asks": [], "bid_usd": 0.0, "ask_usd": 0.0, "imbalance": 0.0}
        if isinstance(bitget_info, Exception):
            bitget_info = {}
        if isinstance(okx_oi, Exception):
            okx_oi = {}

        hl_oi_usd = float(hl_coin.get("open_interest_usd", 0.0))
        bg_oi_usd = float(bitget_info.get("open_interest_usd", 0.0))
        okx_oi_usd = float(okx_oi.get("open_interest_usd", 0.0))
        total_oi_usd = hl_oi_usd + bg_oi_usd + okx_oi_usd

        fundings = []
        if "funding_8h_equiv" in hl_coin:
            fundings.append(float(hl_coin["funding_8h_equiv"]))
        if "funding_rate" in bitget_info:
            fundings.append(float(bitget_info["funding_rate"]))
        avg_funding_8h = sum(fundings) / len(fundings) if fundings else 0.0

        bundle = {
            "symbol": coin,
            "mark_price": float(hl_coin.get("mark_price") or bitget_info.get("mark_price") or 0.0),
            "oracle_price": float(hl_coin.get("oracle_price") or bitget_info.get("index_price") or 0.0),
            "change_24h_pct": float(hl_coin.get("change_24h_pct") or bitget_info.get("change_24h_pct") or 0.0),
            "total_open_interest_usd": total_oi_usd,
            "hl_open_interest_usd": hl_oi_usd,
            "bitget_open_interest_usd": bg_oi_usd,
            "okx_open_interest_usd": okx_oi_usd,
            "avg_funding_8h": avg_funding_8h,
            "hl_funding_8h": float(hl_coin.get("funding_8h_equiv", 0.0)),
            "bitget_funding_8h": float(bitget_info.get("funding_rate", 0.0)),
            "basis_premium": float(hl_coin.get("premium", 0.0)),
            "volume_24h_usd": float(hl_coin.get("volume_24h_usd", 0.0)) + float(bitget_info.get("volume_24h_usd", 0.0)),
            "orderbook": hl_book,
        }
        return self._set_cached(cache_key, bundle)

    async def _fetch_hl_orderbook(self, coin: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(self.HL_INFO_URL, json={"type": "l2Book", "coin": coin})
            resp.raise_for_status()
            data = resp.json()
            levels = data.get("levels", [[], []])
            raw_bids, raw_asks = levels[0], levels[1]

            bids = [{"px": float(b["px"]), "sz": float(b["sz"]), "usd": float(b["px"]) * float(b["sz"])} for b in raw_bids[:20]]
            asks = [{"px": float(a["px"]), "sz": float(a["sz"]), "usd": float(a["px"]) * float(a["sz"])} for a in raw_asks[:20]]

            bid_usd = sum(b["usd"] for b in bids)
            ask_usd = sum(a["usd"] for a in asks)
            denom = bid_usd + ask_usd
            imbalance = ((bid_usd - ask_usd) / denom) if denom > 0 else 0.0

            biggest_bid = max(bids, key=lambda x: x["usd"]) if bids else None
            biggest_ask = max(asks, key=lambda x: x["usd"]) if asks else None

            return {
                "bids": bids,
                "asks": asks,
                "bid_usd": bid_usd,
                "ask_usd": ask_usd,
                "imbalance": imbalance,
                "biggest_bid_wall": biggest_bid,
                "biggest_ask_wall": biggest_ask,
            }

    async def _fetch_bitget_futures(self, coin: str) -> Dict[str, Any]:
        url = f"{self.BITGET_BASE}/api/v2/mix/market/ticker?productType=USDT-FUTURES&symbol={coin}USDT"
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            arr = resp.json().get("data", [])
            if not arr:
                return {}
            d = arr[0]
            mark_px = float(d.get("markPrice") or d.get("lastPr") or 0.0)
            holding_coins = float(d.get("holdingAmount") or 0.0)
            return {
                "mark_price": mark_px,
                "index_price": float(d.get("indexPrice") or mark_px),
                "funding_rate": float(d.get("fundingRate") or 0.0),
                "open_interest_usd": holding_coins * mark_px,
                "volume_24h_usd": float(d.get("usdtVolume") or 0.0),
                "change_24h_pct": float(d.get("change24h") or 0.0) * 100.0,
            }

    async def _fetch_okx_oi(self, coin: str) -> Dict[str, Any]:
        url = f"{self.OKX_BASE}/api/v5/public/open-interest?instType=SWAP&instId={coin}-USDT-SWAP"
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            arr = resp.json().get("data", [])
            if not arr:
                return {}
            return {"open_interest_usd": float(arr[0].get("oiUsd") or 0.0)}


market_hub = MarketDataHub()
