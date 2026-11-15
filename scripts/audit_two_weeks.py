import json
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

DB = Path("data/futures_intelligence.db")
OUT = Path("audit_snapshot.json")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
now = int(time.time())
cutoff = now - 14 * 86400


def rows(sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def scalar(sql, args=(), default=0):
    r = conn.execute(sql, args).fetchone()
    return (r[0] if r and r[0] is not None else default)


def summarize_outcomes(items):
    closed = [x for x in items if x["status"] in ("CLOSED", "TIMEOUT")]
    entered = [x for x in items if x["status"] in ("OPEN", "CLOSED", "TIMEOUT")]
    rs = [float(x["net_r"] or 0) for x in closed]
    wins = [r for r in rs if r > 0]
    stops = [x for x in closed if x["result"] == "SL"]
    return {
        "registered": len(items),
        "pending": sum(x["status"] == "PENDING_ENTRY" for x in items),
        "open": sum(x["status"] == "OPEN" for x in items),
        "no_entry": sum(x["status"] == "NO_ENTRY" for x in items),
        "entered": len(entered),
        "closed": len(closed),
        "tp2": sum(x["result"] == "TP2" for x in closed),
        "stops": len(stops),
        "timeouts": sum(x["result"] == "TIMEOUT" for x in closed),
        "win_rate_pct": round(100 * len(wins) / len(closed), 2) if closed else None,
        "stop_rate_pct": round(100 * len(stops) / len(closed), 2) if closed else None,
        "expectancy_r": round(statistics.mean(rs), 4) if rs else None,
        "median_r": round(statistics.median(rs), 4) if rs else None,
        "avg_mae_r": round(statistics.mean(float(x["mae_r"] or 0) for x in closed), 4) if closed else None,
        "avg_mfe_r": round(statistics.mean(float(x["mfe_r"] or 0) for x in closed), 4) if closed else None,
        "ambiguous": sum(int(x["ambiguous_bar"] or 0) for x in closed),
    }

signals = rows("SELECT * FROM signals WHERE created_at >= ?", (cutoff,))
outcomes = rows(
    """SELECT o.*, s.telegram_sent, s.actionable, s.confidence, s.composite_score, s.payload_json
       FROM signal_outcomes o JOIN signals s ON s.signal_id=o.signal_id
       WHERE o.signal_created_at >= ? ORDER BY o.signal_created_at""",
    (cutoff,),
)
for x in outcomes:
    x["snapshot"] = json.loads(x.get("snapshot_json") or "{}")
    x["payload"] = json.loads(x.get("payload_json") or "{}")

report = {
    "generated_at": now,
    "db_size_bytes": DB.stat().st_size,
    "data_range": {
        "first_signal": min((x["created_at"] for x in signals), default=None),
        "last_signal": max((x["created_at"] for x in signals), default=None),
    },
    "signals": {
        "total": len(signals),
        "market": sum(x["signal_source"] == "MARKET_ANALYSIS" for x in signals),
        "copy": sum(x["signal_source"] == "COPY_TRADER" for x in signals),
        "actionable_market": sum(x["signal_source"] == "MARKET_ANALYSIS" and x["actionable"] for x in signals),
        "sent_market": sum(x["signal_source"] == "MARKET_ANALYSIS" and x["telegram_sent"] for x in signals),
        "sent_copy": sum(x["signal_source"] == "COPY_TRADER" and x["telegram_sent"] for x in signals),
    },
    "outcomes_all_directional": summarize_outcomes(outcomes),
    "outcomes_actionable_creation": summarize_outcomes([x for x in outcomes if x["snapshot"].get("actionable_at_creation")]),
    "outcomes_telegram_sent": summarize_outcomes([x for x in outcomes if x["telegram_sent"]]),
}

for dim, key_fn in {
    "by_symbol": lambda x: x["symbol"],
    "by_direction": lambda x: x["direction"],
    "by_version": lambda x: x["strategy_version"],
    "by_confidence_bucket": lambda x: f"{int(float(x['confidence'])//10)*10}-{int(float(x['confidence'])//10)*10+9}",
}.items():
    groups = defaultdict(list)
    for x in outcomes:
        groups[key_fn(x)].append(x)
    report[dim] = {k: summarize_outcomes(v) for k, v in sorted(groups.items())}

filters = {
    "actionable": lambda s: bool(s.get("actionable_at_creation")),
    "htf_aligned": lambda s: bool(s.get("htf_aligned")),
    "structural_rr_pass": lambda s: bool(s.get("structural_rr_pass")),
    "chase_filter_pass": lambda s: bool(s.get("chase_filter_pass")),
    "fib_confirmed": lambda s: s.get("fib_confluence") in ("BULLISH_CONFIRMED", "BEARISH_CONFIRMED"),
}
report["by_filter_pass"] = {
    name: summarize_outcomes([x for x in outcomes if pred(x["snapshot"])])
    for name, pred in filters.items()
}

# Telegram reliability and scan cadence
logs = rows("SELECT * FROM telegram_logs WHERE created_at >= ? ORDER BY created_at", (cutoff,))
by_type = defaultdict(lambda: {"ok": 0, "fail": 0})
for x in logs:
    by_type[x["msg_type"]]["ok" if x["delivered"] else "fail"] += 1
summaries = [x["created_at"] for x in logs if x["msg_type"] == "AUTO_SCAN_15M" and x["delivered"]]
gaps = [b-a for a,b in zip(summaries, summaries[1:])]
report["telegram"] = {
    "total": len(logs),
    "delivered": sum(x["delivered"] for x in logs),
    "failed": sum(not x["delivered"] for x in logs),
    "by_type": dict(by_type),
    "scan_summaries": len(summaries),
    "median_summary_gap_sec": statistics.median(gaps) if gaps else None,
    "max_summary_gap_sec": max(gaps) if gaps else None,
    "gaps_over_20m": sum(g > 1200 for g in gaps),
    "gaps_over_35m": sum(g > 2100 for g in gaps),
}

# Copy trader state / rotations
report["copy_trader"] = {
    "traders": scalar("SELECT COUNT(*) FROM copy_traders"),
    "active": scalar("SELECT COUNT(*) FROM copy_traders WHERE status='ACTIVE'"),
    "rotated_out": scalar("SELECT COUNT(*) FROM copy_traders WHERE status='ROTATED_OUT'"),
    "rotations_14d": scalar("SELECT COUNT(*) FROM copy_rotation_logs WHERE created_at >= ?", (cutoff,)),
    "signaled_open_positions": scalar("SELECT COUNT(*) FROM copy_signaled_positions"),
}

OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(OUT)
