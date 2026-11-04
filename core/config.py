import json
import os
from pathlib import Path
from typing import Dict, List, Any

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_CONFIG_PATH = DATA_DIR / "runtime_settings.json"


def _parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        values[k] = v
    return values


class SettingsManager:
    """Dynamic configuration manager synced with .env and runtime JSON."""

    DEFAULTS: Dict[str, Any] = {
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
        "TELEGRAM_AUTO_SEND": True,
        "AI_PROVIDER": "auto",
        "GEMINI_API_KEY": "",
        "GEMINI_MODEL": "gemini-2.5-flash",
        "OPENROUTER_API_KEY": "",
        "OPENROUTER_MODEL": "deepseek/deepseek-chat",
        "DEEPSEEK_API_KEY": "",
        "DEEPSEEK_MODEL": "deepseek-chat",
        "OPENAI_API_KEY": "",
        "OPENAI_MODEL": "gpt-4o-mini",
        "WATCHLIST": "BTC,ETH,SOL,XRP,DOGE,SUI,AVAX,LINK",
        "PRIMARY_TIMEFRAME": "1h",
        "HTF_TIMEFRAME": "4h",
        "LTF_TIMEFRAME": "15m",
        "MAX_LEVERAGE": 10,
        "DEFAULT_RISK_PER_TRADE_PCT": 1.5,
        "MIN_CONFIDENCE_SCORE": 60,
        "MIN_RISK_REWARD_RATIO": 1.6,
        "COPY_TRADER_MAX_CONSECUTIVE_LOSSES": 2,
        "COPY_TRADER_MIN_EQUITY_USD": 50000.0,
        "COPY_TRADER_ROSTER_SIZE": 12,
        "HOST": "0.0.0.0",
        "PORT": 8000,
        "AUTO_SCAN_ENABLED": True,
        "AUTO_SCAN_INTERVAL_SEC": 900,
        "SCAN_SYNC_CANDLE_CLOSE": True,
        "COPY_MONITOR_INTERVAL_SEC": 90,
    }

    BOOL_KEYS = {"TELEGRAM_AUTO_SEND", "AUTO_SCAN_ENABLED", "SCAN_SYNC_CANDLE_CLOSE"}
    INT_KEYS = {
        "MAX_LEVERAGE",
        "MIN_CONFIDENCE_SCORE",
        "COPY_TRADER_MAX_CONSECUTIVE_LOSSES",
        "COPY_TRADER_ROSTER_SIZE",
        "PORT",
        "AUTO_SCAN_INTERVAL_SEC",
        "COPY_MONITOR_INTERVAL_SEC",
    }
    FLOAT_KEYS = {
        "DEFAULT_RISK_PER_TRADE_PCT",
        "MIN_RISK_REWARD_RATIO",
        "COPY_TRADER_MIN_EQUITY_USD",
    }
    SECRET_KEYS = {
        "TELEGRAM_BOT_TOKEN",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
    }

    def __init__(self) -> None:
        self._data: Dict[str, Any] = dict(self.DEFAULTS)
        self.reload()

    def _cast(self, key: str, val: Any) -> Any:
        if val is None:
            return self.DEFAULTS.get(key)
        if key in self.BOOL_KEYS:
            if isinstance(val, bool):
                return val
            return str(val).strip().lower() in ("1", "true", "yes", "on")
        if key in self.INT_KEYS:
            try:
                return int(float(val))
            except (ValueError, TypeError):
                return self.DEFAULTS[key]
        if key in self.FLOAT_KEYS:
            try:
                return float(val)
            except (ValueError, TypeError):
                return self.DEFAULTS[key]
        return str(val).strip()

    def reload(self) -> None:
        env_vals = _parse_env_file(ENV_PATH)
        for k in self.DEFAULTS:
            raw = os.environ.get(k, env_vals.get(k, self.DEFAULTS[k]))
            if raw == "" and k not in self.SECRET_KEYS and k != "TELEGRAM_CHAT_ID":
                raw = self.DEFAULTS[k]
            self._data[k] = self._cast(k, raw)

        if RUNTIME_CONFIG_PATH.exists():
            try:
                overrides = json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
                for k, v in overrides.items():
                    if k in self.DEFAULTS:
                        self._data[k] = self._cast(k, v)
            except Exception:
                pass

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def watchlist(self) -> List[str]:
        raw = str(self._data.get("WATCHLIST", "BTC,ETH,SOL"))
        coins = []
        for item in raw.split(","):
            clean = item.strip().upper().replace("USDT", "").replace("-SWAP", "").replace("-PERP", "")
            if clean and clean not in coins:
                coins.append(clean)
        return coins or ["BTC", "ETH", "SOL"]

    def update(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        for k, v in updates.items():
            if k not in self.DEFAULTS:
                continue
            # Preserve existing secret if masked placeholder passed from UI
            if k in self.SECRET_KEYS and isinstance(v, str) and v.startswith("***"):
                continue
            self._data[k] = self._cast(k, v)

        # Persist to runtime_settings.json and sync .env
        RUNTIME_CONFIG_PATH.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self._write_env()
        return self.public_dict()

    def _write_env(self) -> None:
        lines = []
        for k, v in self._data.items():
            if isinstance(v, bool):
                val_str = "true" if v else "false"
            else:
                val_str = str(v)
            lines.append(f"{k}={val_str}")
        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def public_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, v in self._data.items():
            if k in self.SECRET_KEYS:
                s = str(v or "")
                out[k] = f"***{s[-4:]}" if len(s) > 4 else ("***" if s else "")
                out[f"{k}_SET"] = bool(s)
            else:
                out[k] = v
        out["WATCHLIST_ARRAY"] = self.watchlist
        return out


settings = SettingsManager()
