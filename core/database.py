import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import DATA_DIR

DB_PATH = DATA_DIR / "futures_intelligence.db"


class Database:
    """SQLite persistence layer for Signals, Copy-Traders, 2-Strike Rotations, and Telegram Logs."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    signal_id TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    timestamp_iso TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    actionable INTEGER NOT NULL,
                    confidence REAL NOT NULL,
                    composite_score REAL NOT NULL,
                    current_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    tp1 REAL NOT NULL,
                    tp2 REAL NOT NULL,
                    tp3 REAL NOT NULL,
                    recommended_leverage INTEGER NOT NULL,
                    signal_source TEXT NOT NULL,
                    telegram_sent INTEGER DEFAULT 0,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS copy_traders (
                    address TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    rank_order INTEGER NOT NULL,
                    status TEXT NOT NULL, -- ACTIVE | STANDBY | ROTATED_OUT
                    account_value_usd REAL DEFAULT 0,
                    pnl_day_usd REAL DEFAULT 0,
                    pnl_week_usd REAL DEFAULT 0,
                    pnl_month_usd REAL DEFAULT 0,
                    roi_month_pct REAL DEFAULT 0,
                    win_rate_pct REAL DEFAULT 0,
                    smart_score REAL DEFAULT 0,
                    consecutive_losses INTEGER DEFAULT 0,
                    copied_wins INTEGER DEFAULT 0,
                    copied_losses INTEGER DEFAULT 0,
                    last_fill_tid INTEGER DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    extra_json TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS copy_rotation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    timestamp_iso TEXT NOT NULL,
                    old_trader_address TEXT NOT NULL,
                    old_trader_label TEXT NOT NULL,
                    reason_fa TEXT NOT NULL,
                    consecutive_losses INTEGER NOT NULL,
                    new_trader_address TEXT,
                    new_trader_label TEXT,
                    details_json TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS telegram_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    timestamp_iso TEXT NOT NULL,
                    msg_type TEXT NOT NULL,
                    symbol TEXT,
                    chat_id TEXT,
                    delivered INTEGER NOT NULL,
                    delivery_note TEXT,
                    message_text TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS copy_signaled_positions (
                    pos_key TEXT PRIMARY KEY,
                    trader_address TEXT NOT NULL,
                    coin TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )
            conn.commit()

    def save_signal(self, signal_dict: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO signals (
                    signal_id, created_at, timestamp_iso, symbol, timeframe,
                    direction, actionable, confidence, composite_score,
                    current_price, stop_loss, tp1, tp2, tp3,
                    recommended_leverage, signal_source, telegram_sent, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_dict["signal_id"],
                    int(time.time()),
                    signal_dict["timestamp_iso"],
                    signal_dict["symbol"],
                    signal_dict["timeframe"],
                    signal_dict["direction"],
                    1 if signal_dict.get("actionable") else 0,
                    float(signal_dict["confidence"]),
                    float(signal_dict["composite_score"]),
                    float(signal_dict["current_price"]),
                    float(signal_dict["stop_loss"]),
                    float(signal_dict["tp1"]),
                    float(signal_dict["tp2"]),
                    float(signal_dict["tp3"]),
                    int(signal_dict["recommended_leverage"]),
                    signal_dict.get("signal_source", "MULTI_AGENT_CONFLUENCE"),
                    1 if signal_dict.get("telegram_sent") else 0,
                    json.dumps(signal_dict, ensure_ascii=False),
                ),
            )
            conn.commit()

    def mark_signal_telegram_sent(self, signal_id: str) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM signals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
            if row:
                data = json.loads(row["payload_json"])
                data["telegram_sent"] = True
                conn.execute(
                    "UPDATE signals SET telegram_sent = 1, payload_json = ? WHERE signal_id = ?",
                    (json.dumps(data, ensure_ascii=False), signal_id),
                )
                conn.commit()

    def get_last_sent_market_signal(
        self, symbol: str, direction: str, exclude_signal_id: str = ""
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT created_at, current_price, confidence
                FROM signals
                WHERE symbol = ?
                  AND direction = ?
                  AND signal_source = 'MARKET_ANALYSIS'
                  AND telegram_sent = 1
                  AND signal_id != ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (symbol.upper(), direction.upper(), exclude_signal_id),
            ).fetchone()
            return dict(row) if row else None

    def list_signals(
        self,
        limit: int = 30,
        actionable_only: bool = False,
        signal_source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if actionable_only:
            clauses.append("actionable = 1")
        if signal_source:
            clauses.append("signal_source = ?")
            params.append(signal_source)

        query = "SELECT payload_json FROM signals"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._lock, self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def is_copy_position_signaled(self, pos_key: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM copy_signaled_positions WHERE pos_key = ?", (pos_key,)
            ).fetchone()
            return row is not None

    def get_copy_signaled_position(self, pos_key: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM copy_signaled_positions WHERE pos_key = ?", (pos_key,)
            ).fetchone()
            return dict(row) if row else None

    def get_copy_signaled_positions_for_trader(self, trader_address: str) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM copy_signaled_positions WHERE trader_address = ?", (trader_address,)
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_copy_signaled_position(self, pos_key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM copy_signaled_positions WHERE pos_key = ?", (pos_key,))
            conn.commit()

    def mark_copy_position_signaled(
        self, pos_key: str, trader_address: str, coin: str, direction: str, entry_price: float
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO copy_signaled_positions (
                    pos_key, trader_address, coin, direction, entry_price, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (pos_key, trader_address, coin, direction, float(entry_price), int(time.time())),
            )
            conn.commit()

    def get_previous_signals_for_symbol(
        self, symbol: str, timeframe: Optional[str] = None, limit: int = 5
    ) -> List[Dict[str, Any]]:
        coin = symbol.upper().replace("USDT", "").strip()
        with self._lock, self._connect() as conn:
            if timeframe:
                rows = conn.execute(
                    "SELECT payload_json FROM signals WHERE symbol = ? AND timeframe = ? AND signal_source != 'COPY_TRADER' ORDER BY created_at DESC LIMIT ?",
                    (coin, timeframe, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM signals WHERE symbol = ? AND signal_source != 'COPY_TRADER' ORDER BY created_at DESC LIMIT ?",
                    (coin, limit),
                ).fetchall()
            return [json.loads(r["payload_json"]) for r in rows]

    def upsert_trader(self, trader: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT status, consecutive_losses, copied_wins, copied_losses, last_fill_tid FROM copy_traders WHERE address = ?",
                (trader["address"],),
            ).fetchone()

            status = trader.get("status")
            consec_losses = trader.get("consecutive_losses", 0)
            copied_wins = trader.get("copied_wins", 0)
            copied_losses = trader.get("copied_losses", 0)
            last_fill_tid = trader.get("last_fill_tid", 0)

            if existing and not trader.get("_force_state", False):
                status = existing["status"]
                consec_losses = existing["consecutive_losses"]
                copied_wins = existing["copied_wins"]
                copied_losses = existing["copied_losses"]
                last_fill_tid = existing["last_fill_tid"] or last_fill_tid

            extra = {
                "open_positions": trader.get("open_positions", []),
                "recent_closed_trades": trader.get("recent_closed_trades", []),
            }
            conn.execute(
                """
                INSERT INTO copy_traders (
                    address, label, rank_order, status, account_value_usd,
                    pnl_day_usd, pnl_week_usd, pnl_month_usd, roi_month_pct,
                    win_rate_pct, smart_score, consecutive_losses, copied_wins,
                    copied_losses, last_fill_tid, updated_at, extra_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    label=excluded.label,
                    rank_order=excluded.rank_order,
                    status=excluded.status,
                    account_value_usd=excluded.account_value_usd,
                    pnl_day_usd=excluded.pnl_day_usd,
                    pnl_week_usd=excluded.pnl_week_usd,
                    pnl_month_usd=excluded.pnl_month_usd,
                    roi_month_pct=excluded.roi_month_pct,
                    win_rate_pct=excluded.win_rate_pct,
                    smart_score=excluded.smart_score,
                    consecutive_losses=excluded.consecutive_losses,
                    copied_wins=excluded.copied_wins,
                    copied_losses=excluded.copied_losses,
                    last_fill_tid=excluded.last_fill_tid,
                    updated_at=excluded.updated_at,
                    extra_json=excluded.extra_json
                """,
                (
                    trader["address"],
                    trader["label"],
                    int(trader.get("rank", 99)),
                    status or "STANDBY",
                    float(trader.get("account_value_usd", 0)),
                    float(trader.get("pnl_day_usd", 0)),
                    float(trader.get("pnl_week_usd", 0)),
                    float(trader.get("pnl_month_usd", 0)),
                    float(trader.get("roi_month_pct", 0)),
                    float(trader.get("win_rate_pct", 0)),
                    float(trader.get("smart_score", 0)),
                    int(consec_losses),
                    int(copied_wins),
                    int(copied_losses),
                    int(last_fill_tid),
                    int(time.time()),
                    json.dumps(extra, ensure_ascii=False),
                ),
            )
            conn.commit()

    def get_traders(self) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM copy_traders
                ORDER BY
                    CASE status
                        WHEN 'ACTIVE' THEN 0
                        WHEN 'STANDBY' THEN 1
                        ELSE 2
                    END,
                    rank_order ASC,
                    smart_score DESC
                """
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                extra = json.loads(d.pop("extra_json", "{}") or "{}")
                d["rank"] = d.pop("rank_order", 1)
                d["open_positions"] = extra.get("open_positions", [])
                d["recent_closed_trades"] = extra.get("recent_closed_trades", [])
                out.append(d)
            return out

    def get_active_trader(self) -> Optional[Dict[str, Any]]:
        traders = self.get_traders()
        for t in traders:
            if t["status"] == "ACTIVE":
                return t
        return None

    def update_trader_state(
        self,
        address: str,
        *,
        status: Optional[str] = None,
        consecutive_losses: Optional[int] = None,
        copied_wins: Optional[int] = None,
        copied_losses: Optional[int] = None,
        last_fill_tid: Optional[int] = None,
        open_positions: Optional[List[Dict[str, Any]]] = None,
        recent_closed_trades: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM copy_traders WHERE address = ?", (address,)
            ).fetchone()
            if not row:
                return
            extra = json.loads(row["extra_json"] or "{}")
            if open_positions is not None:
                extra["open_positions"] = open_positions
            if recent_closed_trades is not None:
                extra["recent_closed_trades"] = recent_closed_trades

            conn.execute(
                """
                UPDATE copy_traders SET
                    status = COALESCE(?, status),
                    consecutive_losses = COALESCE(?, consecutive_losses),
                    copied_wins = COALESCE(?, copied_wins),
                    copied_losses = COALESCE(?, copied_losses),
                    last_fill_tid = COALESCE(?, last_fill_tid),
                    extra_json = ?,
                    updated_at = ?
                WHERE address = ?
                """,
                (
                    status,
                    consecutive_losses,
                    copied_wins,
                    copied_losses,
                    last_fill_tid,
                    json.dumps(extra, ensure_ascii=False),
                    int(time.time()),
                    address,
                ),
            )
            conn.commit()

    def log_rotation(
        self,
        timestamp_iso: str,
        old_address: str,
        old_label: str,
        reason_fa: str,
        consecutive_losses: int,
        new_address: Optional[str],
        new_label: Optional[str],
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO copy_rotation_logs (
                    created_at, timestamp_iso, old_trader_address, old_trader_label,
                    reason_fa, consecutive_losses, new_trader_address, new_trader_label, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time()),
                    timestamp_iso,
                    old_address,
                    old_label,
                    reason_fa,
                    consecutive_losses,
                    new_address,
                    new_label,
                    json.dumps(details or {}, ensure_ascii=False),
                ),
            )
            conn.commit()

    def list_rotations(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM copy_rotation_logs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["details"] = json.loads(d.pop("details_json", "{}") or "{}")
                out.append(d)
            return out

    def log_telegram(
        self,
        timestamp_iso: str,
        msg_type: str,
        symbol: Optional[str],
        chat_id: str,
        delivered: bool,
        delivery_note: str,
        message_text: str,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_logs (
                    created_at, timestamp_iso, msg_type, symbol, chat_id,
                    delivered, delivery_note, message_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time()),
                    timestamp_iso,
                    msg_type,
                    symbol,
                    chat_id,
                    1 if delivered else 0,
                    delivery_note,
                    message_text,
                ),
            )
            conn.commit()

    def list_telegram_logs(self, limit: int = 25) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM telegram_logs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]


db = Database()
