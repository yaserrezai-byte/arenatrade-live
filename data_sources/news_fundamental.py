import asyncio
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List
import httpx


class NewsAndFundamentalHub:
    """
    Fetches real-time Crypto News RSS feeds (CoinTelegraph, CoinDesk, Decrypt),
    Fear & Greed Index (Alternative.me), and CoinGecko Global & Asset Fundamentals.
    """

    RSS_FEEDS = [
        ("CoinTelegraph", "https://cointelegraph.com/rss"),
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("Decrypt", "https://decrypt.co/feed"),
    ]

    COINGECKO_IDS = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
        "XRP": "ripple",
        "DOGE": "dogecoin",
        "SUI": "sui",
        "AVAX": "avalanche-2",
        "LINK": "chainlink",
        "BNB": "binancecoin",
        "ADA": "cardano",
        "TON": "the-open-network",
        "ARB": "arbitrum",
        "OP": "optimism",
        "PEPE": "pepe",
        "HYPE": "hyperliquid",
    }

    SYMBOL_KEYWORDS = {
        "BTC": ["bitcoin", "btc", "etf", "microstrategy", "satoshi", "halving"],
        "ETH": ["ethereum", "eth", "vitalik", "layer 2", "staking", "sec"],
        "SOL": ["solana", "sol", "firedancer", "pump.fun", "jupiter"],
        "XRP": ["xrp", "ripple", "rlusd", "garlinghouse"],
        "DOGE": ["dogecoin", "doge", "musk"],
        "SUI": ["sui", "mysten"],
        "AVAX": ["avalanche", "avax"],
        "LINK": ["chainlink", "link", "ccip", "oracle"],
    }

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}
        self._cache_ts: Dict[str, float] = {}

    def _get(self, key: str, ttl: float) -> Any:
        if key in self._cache and (time.time() - self._cache_ts.get(key, 0)) < ttl:
            return self._cache[key]
        return None

    def _set(self, key: str, val: Any) -> Any:
        self._cache[key] = val
        self._cache_ts[key] = time.time()
        return val

    async def fetch_news(self, symbol: str = "BTC") -> List[Dict[str, Any]]:
        all_items = self._get("all_rss_news", 180.0)
        if all_items is None:
            async with httpx.AsyncClient(timeout=9.0, follow_redirects=True) as client:
                tasks = [
                    client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                    for _, url in self.RSS_FEEDS
                ]
                resps = await asyncio.gather(*tasks, return_exceptions=True)

            parsed: List[Dict[str, Any]] = []
            for (source_name, _), resp in zip(self.RSS_FEEDS, resps):
                if isinstance(resp, Exception) or resp.status_code != 200:
                    continue
                try:
                    root = ET.fromstring(resp.text)
                    for item in root.findall(".//item")[:18]:
                        title = (item.findtext("title") or "").strip()
                        link = (item.findtext("link") or "").strip()
                        pub_date = (item.findtext("pubDate") or "").strip()
                        desc_raw = item.findtext("description") or ""
                        desc = re.sub(r"<[^>]+>", "", desc_raw).strip()[:240]
                        if title:
                            parsed.append(
                                {
                                    "source": source_name,
                                    "title": title,
                                    "summary": desc,
                                    "url": link,
                                    "pub_date": pub_date,
                                }
                            )
                except Exception:
                    continue
            all_items = self._set("all_rss_news", parsed)

        coin = symbol.upper().replace("USDT", "")
        kws = self.SYMBOL_KEYWORDS.get(coin, [coin.lower()])
        macro_kws = ["fed", "cpi", "inflation", "rate", "sec", "tariff", "crypto", "market", "liquidat", "etf", "whale"]

        specific = []
        macro = []
        for n in all_items:
            hay = f"{n['title']} {n['summary']}".lower()
            if any(k in hay for k in kws):
                n_copy = dict(n, relevance="ASSET_SPECIFIC")
                specific.append(n_copy)
            elif any(mk in hay for mk in macro_kws):
                n_copy = dict(n, relevance="MACRO_MARKET")
                macro.append(n_copy)

        return (specific + macro)[:14]

    async def fetch_fundamentals(self, symbol: str = "BTC") -> Dict[str, Any]:
        coin = symbol.upper().replace("USDT", "")

        fng_task = self._fetch_fng()
        global_task = self._fetch_cg_global()
        markets_task = self._fetch_cg_markets()

        fng, cg_global, cg_markets = await asyncio.gather(
            fng_task, global_task, markets_task, return_exceptions=True
        )
        if isinstance(fng, Exception):
            fng = {"value": 50, "classification": "Neutral"}
        if isinstance(cg_global, Exception):
            cg_global = {}
        if isinstance(cg_markets, Exception):
            cg_markets = {}

        cg_id = self.COINGECKO_IDS.get(coin, coin.lower())
        coin_info = cg_markets.get(cg_id) or cg_markets.get(coin.lower()) or {}

        return {
            "symbol": coin,
            "fear_greed_value": fng.get("value", 50),
            "fear_greed_label": fng.get("classification", "Neutral"),
            "btc_dominance": cg_global.get("btc_dominance", 55.0),
            "eth_dominance": cg_global.get("eth_dominance", 10.0),
            "market_cap_change_24h_pct": cg_global.get("mcap_change_24h", 0.0),
            "coin_market_cap_usd": coin_info.get("market_cap", 0.0),
            "coin_fdv_usd": coin_info.get("fully_diluted_valuation") or 0.0,
            "coin_volume_24h_usd": coin_info.get("total_volume", 0.0),
            "ath_change_pct": coin_info.get("ath_change_percentage", 0.0),
            "price_change_7d_pct": coin_info.get("price_change_percentage_7d_in_currency") or 0.0,
            "market_cap_rank": coin_info.get("market_cap_rank") or 0,
        }

    async def _fetch_fng(self) -> Dict[str, Any]:
        cached = self._get("fng", 600.0)
        if cached:
            return cached
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get("https://api.alternative.me/fng/?limit=2")
            resp.raise_for_status()
            arr = resp.json().get("data", [])
            if not arr:
                return {"value": 50, "classification": "Neutral"}
            res = {
                "value": int(arr[0].get("value", 50)),
                "classification": arr[0].get("value_classification", "Neutral"),
            }
            return self._set("fng", res)

    async def _fetch_cg_global(self) -> Dict[str, Any]:
        cached = self._get("cg_global", 600.0)
        if cached:
            return cached
        async with httpx.AsyncClient(timeout=9.0) as client:
            resp = await client.get("https://api.coingecko.com/api/v3/global")
            resp.raise_for_status()
            d = resp.json().get("data", {})
            pct = d.get("market_cap_percentage", {})
            res = {
                "btc_dominance": round(float(pct.get("btc", 55.0)), 2),
                "eth_dominance": round(float(pct.get("eth", 10.0)), 2),
                "mcap_change_24h": round(float(d.get("market_cap_change_percentage_24h_usd", 0.0)), 2),
            }
            return self._set("cg_global", res)

    async def _fetch_cg_markets(self) -> Dict[str, Dict[str, Any]]:
        cached = self._get("cg_markets", 600.0)
        if cached:
            return cached
        url = (
            "https://api.coingecko.com/api/v3/coins/markets"
            "?vs_currency=usd&order=market_cap_desc&per_page=60&page=1"
            "&sparkline=false&price_change_percentage=24h,7d"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            rows = resp.json()
            mapped: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                mapped[str(r.get("id", "")).lower()] = r
                mapped[str(r.get("symbol", "")).lower()] = r
            return self._set("cg_markets", mapped)


news_fundamental_hub = NewsAndFundamentalHub()
