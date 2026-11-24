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

for suffix, subset in {
    "sent": [x for x in outcomes if x["telegram_sent"]],
    "actionable": [x for x in outcomes if x["snapshot"].get("actionable_at_creation")],
}.items():
    for dim, key_fn in {
        "symbol": lambda x: x["symbol"],
        "direction": lambda x: x["direction"],
    }.items():
        groups = defaultdict(list)
        for x in subset:
            groups[key_fn(x)].append(x)
        report[f"by_{dim}_{suffix}"] = {k: summarize_outcomes(v) for k, v in sorted(groups.items())}

# Quantify duplicate/overlapping alerts for the same symbol. A new alert while an older
# sent signal is still unresolved is not an independent trade opportunity.
sent_sorted = sorted([x for x in outcomes if x["telegram_sent"]], key=lambda x: x["signal_created_at"])
active_until = {}
overlap_count = 0
sent_gaps_by_symbol = defaultdict(list)
last_sent = {}
for x in sent_sorted:
    sym = x["symbol"]
    ts = int(x["signal_created_at"])
    if sym in last_sent:
        sent_gaps_by_symbol[sym].append(ts - last_sent[sym])
    last_sent[sym] = ts
    if ts < active_until.get(sym, 0):
        overlap_count += 1
    end_ts = int(x.get("exit_at") or (ts + 96 * 900))
    active_until[sym] = max(active_until.get(sym, 0), end_ts)
report["alert_overlap"] = {
    "sent_with_outcomes": len(sent_sorted),
    "overlapping_same_symbol": overlap_count,
    "overlap_pct": round(100 * overlap_count / len(sent_sorted), 2) if sent_sorted else None,
    "median_gap_sec_by_symbol": {
        k: statistics.median(v) for k, v in sent_gaps_by_symbol.items() if v
    },
}

# Daily performance exposes regime concentration and whether one short period dominates.
daily = defaultdict(list)
for x in outcomes:
    if x["status"] in ("CLOSED", "TIMEOUT") and x["telegram_sent"]:
        day = time.strftime("%Y-%m-%d", time.gmtime(int(x["signal_created_at"])))
        daily[day].append(x)
report["daily_sent"] = {k: summarize_outcomes(v) for k, v in sorted(daily.items())}

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

# Version-specific delivered performance (the aggregate version cohort includes
# watch-only observations and cannot explain what users actually received).
version_sent = defaultdict(list)
for x in outcomes:
    if x["telegram_sent"]:
        version_sent[x["strategy_version"]].append(x)
report["by_version_sent"] = {
    k: summarize_outcomes(v) for k, v in sorted(version_sent.items())
}

# Diagnose why the newest strategy is or is not sending, and whether individual
# agents add useful selectivity when they agree with the final direction.
latest_version = max(
    ((x["strategy_version"], int(x["signal_created_at"])) for x in outcomes),
    key=lambda z: z[1],
    default=(None, 0),
)[0]
latest_version_items = [x for x in outcomes if x["strategy_version"] == latest_version]
latest_closed = [x for x in latest_version_items if x["status"] in ("CLOSED", "TIMEOUT")]
agent_agreement = defaultdict(list)
agent_opposition = defaultdict(list)
for x in latest_closed:
    for rep in x["payload"].get("agent_reports", []):
        aid = rep.get("agent_id", "unknown")
        rep_dir = rep.get("direction", "NEUTRAL")
        if rep_dir == x["direction"]:
            agent_agreement[aid].append(x)
        elif rep_dir in ("LONG", "SHORT"):
            agent_opposition[aid].append(x)
report["latest_version_diagnostics"] = {
    "version": latest_version,
    "all": summarize_outcomes(latest_version_items),
    "sent": summarize_outcomes([x for x in latest_version_items if x["telegram_sent"]]),
    "actionable": summarize_outcomes([x for x in latest_version_items if x["snapshot"].get("actionable_at_creation")]),
    "agent_agreement_cohorts": {
        aid: summarize_outcomes(items) for aid, items in sorted(agent_agreement.items())
    },
    "agent_opposition_cohorts": {
        aid: summarize_outcomes(items) for aid, items in sorted(agent_opposition.items())
    },
}

recent_market = sorted(
    [x for x in signals if x["signal_source"] == "MARKET_ANALYSIS"],
    key=lambda x: int(x["created_at"]), reverse=True,
)
latest_by_symbol = {}
veto_counts = Counter()
for row in recent_market:
    payload = json.loads(row.get("payload_json") or "{}")
    if payload.get("strategy_version") != latest_version:
        continue
    for reason in (payload.get("previous_comparison") or {}).get("safety_veto_reasons", []):
        if "MA20/MA60" in reason:
            veto_counts["MA20_MA60"] += 1
        elif "۴ساعته" in reason or "4H" in reason:
            veto_counts["HTF_4H"] += 1
        elif "R:R" in reason:
            veto_counts["STRUCTURAL_RR"] += 1
        elif "فاصله گرفته" in reason:
            veto_counts["CHASE"] += 1
        else:
            veto_counts["AGENT_OR_OTHER"] += 1
    sym = payload.get("symbol")
    if sym and sym not in latest_by_symbol:
        prev = payload.get("previous_comparison") or {}
        latest_by_symbol[sym] = {
            "created_at": row["created_at"],
            "direction": payload.get("direction"),
            "actionable": bool(payload.get("actionable")),
            "telegram_sent": bool(row.get("telegram_sent")),
            "confidence": payload.get("confidence"),
            "net_rr": payload.get("risk_reward_tp2"),
            "ma20_60_alignment": prev.get("ma20_60_alignment"),
            "htf_4h_regime": prev.get("htf_4h_regime"),
            "veto_reasons": prev.get("safety_veto_reasons", []),
        }
report["latest_scan_state"] = {
    "version": latest_version,
    "veto_counts_all_stored_observations": dict(veto_counts),
    "latest_by_symbol": latest_by_symbol,
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
trader_rows = rows("SELECT address,label,status,rank_order,win_rate_pct,consecutive_losses,copied_wins,copied_losses,updated_at FROM copy_traders ORDER BY rank_order")
report["copy_trader"] = {
    "traders": len(trader_rows),
    "active": sum(x["status"] == "ACTIVE" for x in trader_rows),
    "rotated_out": sum(x["status"] == "ROTATED_OUT" for x in trader_rows),
    "rotations_14d": scalar("SELECT COUNT(*) FROM copy_rotation_logs WHERE created_at >= ?", (cutoff,)),
    "signaled_open_positions": scalar("SELECT COUNT(*) FROM copy_signaled_positions"),
    "total_counted_wins": sum(int(x["copied_wins"] or 0) for x in trader_rows),
    "total_counted_losses": sum(int(x["copied_losses"] or 0) for x in trader_rows),
    "max_consecutive_losses": max((int(x["consecutive_losses"] or 0) for x in trader_rows), default=0),
    "roster": trader_rows,
}

OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(OUT)
