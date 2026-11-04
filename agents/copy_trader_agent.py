import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agents.base_agent import BaseAgent
from agents.telegram_agent import telegram_agent
from core.config import settings
from core.database import db
from core.models import AgentReport, FuturesSignal
from data_sources.hyperliquid_copy import hl_copy_source
from data_sources.market_data import market_hub


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class CopyTraderAgent(BaseAgent):
    """
    Independent Smart Copy-Trader Signal Engine (100% Separated from Market Analysis):
    1. Discovers & ranks top successful futures traders from Hyperliquid Mainnet Leaderboard.
    2. Tracks the #1 Active Master Trader's open positions & emits standalone 'COPY_TRADER'
       signals directly to the Telegram Channel whenever new positions open (or on demand).
    3. Enforces the 2-Consecutive-Mistakes Rule:
       - Win  -> resets `consecutive_losses` to 0
       - Loss -> increments `consecutive_losses` (+1)
       - 2 Consecutive Losses -> immediately marks trader `ROTATED_OUT`, promotes the next
         top trader from the `STANDBY` queue, logs the event, and sends a Telegram alert!
    """

    agent_id = "copy_trader"
    agent_name_fa = "ایجنت مستقل کپی‌تریدر هوشمند (قانون ۲ خطا)"
    category_fa = "سیگنال‌دهی مستقل از نهنگ‌های برتر و چرخش خودکار"

    async def refresh_leaderboard_roster(self, force_reseed: bool = False) -> List[Dict[str, Any]]:
        roster_size = int(settings.get("COPY_TRADER_ROSTER_SIZE", 12))
        existing = db.get_traders()

        if existing and not force_reseed:
            await self.monitor_active_trader(auto_emit_new_signals=False)
            return db.get_traders()

        candidates = await hl_copy_source.fetch_leaderboard_candidates(top_n=roster_size)
        existing_map = {t["address"]: t for t in existing}
        has_active = any(t["status"] == "ACTIVE" for t in existing)

        # Prefer activating the highest-ranked top trader who currently has open positions so copy-trading is immediately live
        best_initial_idx = next((i for i, c in enumerate(candidates) if c.get("open_positions")), 0)

        for idx, cand in enumerate(candidates):
            addr = cand["address"]
            if addr in existing_map:
                cand["status"] = existing_map[addr]["status"]
            else:
                if not has_active and idx == best_initial_idx:
                    cand["status"] = "ACTIVE"
                    has_active = True
                else:
                    cand["status"] = "STANDBY"
            db.upsert_trader(cand)

        traders = db.get_traders()
        if traders and not any(t["status"] == "ACTIVE" for t in traders):
            standby = [t for t in traders if t["status"] == "STANDBY"]
            target = standby[0] if standby else traders[0]
            db.update_trader_state(target["address"], status="ACTIVE", consecutive_losses=0)

        return db.get_traders()

    async def build_copy_signal(
        self, trader: Dict[str, Any], pos: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Builds a 100% standalone Copy-Trader FuturesSignal from a whale's live position."""
        coin = str(pos["coin"]).upper()
        direction = "LONG" if str(pos["direction"]).upper() == "LONG" else "SHORT"
        whale_entry = float(pos.get("entry_price") or 0.0)
        whale_lev = int(pos.get("leverage") or 3)
        max_lev_cfg = int(settings.get("MAX_LEVERAGE", 10))
        rec_lev = max(2, min(max_lev_cfg, whale_lev))

        # Fetch live mark price on Hyperliquid if available
        curr_px = whale_entry
        try:
            hl_ctxs = await market_hub.get_hl_all_contexts()
            if coin in hl_ctxs and hl_ctxs[coin].get("mark_price", 0) > 0:
                curr_px = float(hl_ctxs[coin]["mark_price"])
        except Exception:
            pass

        ref_px = curr_px if curr_px > 0 else whale_entry
        # Protective risk distance (~2.2% default, scaled slightly for lower leverage swing trades)
        risk_pct_dist = 0.022 if rec_lev >= 5 else 0.032

        if direction == "LONG":
            entry_low = min(ref_px, whale_entry) * 0.9985
            entry_high = ref_px * 1.0015
            stop_loss = ref_px * (1.0 - risk_pct_dist)
            risk_abs = max(ref_px - stop_loss, ref_px * 0.005)
            tp1 = ref_px + 1.4 * risk_abs
            tp2 = ref_px + 2.4 * risk_abs
            tp3 = ref_px + 3.8 * risk_abs
            inval_fa = f"عبور قیمت به زیر حد ضرر محافظتی ${self.fmt_px(stop_loss)} یا بسته شدن پوزیشن توسط تریدر مرجع."
        else:
            entry_low = ref_px * 0.9985
            entry_high = max(ref_px, whale_entry) * 1.0015
            stop_loss = ref_px * (1.0 + risk_pct_dist)
            risk_abs = max(stop_loss - ref_px, ref_px * 0.005)
            tp1 = ref_px - 1.4 * risk_abs
            tp2 = ref_px - 2.4 * risk_abs
            tp3 = ref_px - 3.8 * risk_abs
            inval_fa = f"عبور قیمت به بالای حد ضرر محافظتی ${self.fmt_px(stop_loss)} یا بسته شدن پوزیشن توسط تریدر مرجع."

        rr_tp2 = round(abs(tp2 - ref_px) / max(risk_abs, 1e-9), 2)
        max_losses = int(settings.get("COPY_TRADER_MAX_CONSECUTIVE_LOSSES", 2))
        consec = int(trader.get("consecutive_losses", 0))
        win_rate = float(trader.get("win_rate_pct", 65.0))
        conf = round(min(95.0, max(58.0, win_rate * 0.85 + 15.0 - consec * 8.0)), 1)

        ts_iso = _now_iso()
        sig_obj = FuturesSignal(
            signal_id=f"COPY-{coin}-{int(datetime.now(timezone.utc).timestamp())}-{uuid.uuid4().hex[:4].upper()}",
            timestamp_iso=ts_iso,
            symbol=coin,
            pair=f"{coin}USDT.P",
            timeframe="LIVE-COPY",
            direction=direction,
            actionable=True,
            signal_source="COPY_TRADER",
            confidence=conf,
            composite_score=round(conf if direction == "LONG" else -conf, 1),
            current_price=ref_px,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            risk_reward_tp2=rr_tp2,
            recommended_leverage=rec_lev,
            margin_mode="ISOLATED",
            risk_pct=float(settings.get("DEFAULT_RISK_PER_TRADE_PCT", 1.5)),
            executive_summary_fa=(
                f"سیگنال مستقل کپی‌تریدر بر اساس پوزیشن باز تریدر موفق {trader['label']} "
                f"(وین‌ریت {win_rate:.1f}%، بازدهی ماهانه +{trader.get('roi_month_pct', 0)}%، خطای متوالی {consec}/{max_losses})."
            ),
            invalidation_note_fa=inval_fa,
            ai_engine_used="Smart-CopyTrader-Engine (Hyperliquid Live)",
            copied_trader_address=trader["address"],
            copied_trader_label=trader["label"],
            sr_zones_and_trendlines={},
            previous_comparison={
                "is_copy_signal": True,
                "whale_equity_usd": trader.get("account_value_usd", 0.0),
                "whale_roi_month_pct": trader.get("roi_month_pct", 0.0),
                "whale_win_rate_pct": win_rate,
                "whale_position_value_usd": pos.get("position_value_usd", 0.0),
                "whale_entry_price": whale_entry,
                "whale_leverage": whale_lev,
                "whale_unrealized_pnl_usd": pos.get("unrealized_pnl_usd", 0.0),
                "whale_liquidation_price": pos.get("liquidation_price"),
                "consecutive_losses": consec,
                "max_allowed_losses": max_losses,
            },
            agent_reports=[],
            telegram_sent=False,
        )
        return sig_obj.model_dump()

    async def monitor_active_trader(self, auto_emit_new_signals: bool = True) -> Dict[str, Any]:
        """
        Polls the Top-3 Elite Whales (#1 ACTIVE Master + top 2 qualified Elite Whales with WR >= 58%):
        1) Enforces the 2-consecutive-losses auto-rotation rule on every monitored whale.
        2) Detects when a whale closes a previously signaled trade -> sends Telegram Exit Alert & unlocks re-entry.
        3) Automatically emits & delivers standalone Copy-Trader signals to Telegram for every newly discovered position
           (marking as signaled ONLY after confirmed Telegram HTTP 200 delivery!).
        """
        all_traders = db.get_traders()
        if not all_traders:
            all_traders = await self.refresh_leaderboard_roster(force_reseed=True)

        active = next((t for t in all_traders if t["status"] == "ACTIVE"), None)
        if not active and all_traders:
            cand = next((t for t in all_traders if t["status"] != "ROTATED_OUT"), all_traders[0])
            db.update_trader_state(cand["address"], status="ACTIVE", consecutive_losses=0)
            all_traders = db.get_traders()
            active = next((t for t in all_traders if t["status"] == "ACTIVE"), None)

        if not active:
            return {"status": "no_traders", "active": None, "rotated": False, "emitted_signals": []}

        max_losses = int(settings.get("COPY_TRADER_MAX_CONSECUTIVE_LOSSES", 2))

        # Build Top-3 Elite Squad: #1 ACTIVE + up to 2 top STANDBY whales with high win-rate (>=58%) and <2 losses
        elite_standby = [
            t
            for t in all_traders
            if t["status"] == "STANDBY"
            and t["address"] != active["address"]
            and float(t.get("win_rate_pct", 0.0)) >= 58.0
            and float(t.get("roi_month_pct", 0.0)) >= 40.0
            and int(t.get("consecutive_losses", 0)) < max_losses
        ][:2]
        squad = [active] + elite_standby

        emitted: List[Dict[str, Any]] = []
        rotated_any = False
        rotation_event = None

        for idx, whale in enumerate(squad):
            is_primary = idx == 0
            addr = whale["address"]
            last_tid = int(whale.get("last_fill_tid") or 0)

            try:
                open_pos, closed_events, win_rate, newest_tid = await hl_copy_source.fetch_trader_state_and_fills(addr)
            except Exception:
                continue

            consec_losses = int(whale.get("consecutive_losses", 0))
            copied_wins = int(whale.get("copied_wins", 0))
            copied_losses = int(whale.get("copied_losses", 0))

            # 1. Check newly closed fills for 2-Consecutive-Mistakes rule
            whale_rotated = False
            if last_tid > 0 and closed_events:
                new_events = [e for e in closed_events if int(e["tid"]) > last_tid]
                new_events.sort(key=lambda x: x["time"])
                for ev in new_events:
                    if ev["closed_pnl"] < 0:
                        consec_losses += 1
                        copied_losses += 1
                        if consec_losses >= max_losses:
                            if is_primary:
                                rot = await self._rotate_active_trader(
                                    old_trader=whale,
                                    consec_losses=consec_losses,
                                    copied_wins=copied_wins,
                                    copied_losses=copied_losses,
                                    latest_tid=max(newest_tid, ev["tid"]),
                                    open_pos=open_pos,
                                    closed_events=closed_events[:12],
                                    reason_fa=f"بسته شدن معامله {ev['coin']} با ضرر ${abs(ev['closed_pnl']):,.2f} (دومین ضرر متوالی)",
                                )
                                rotated_any = True
                                rotation_event = rot
                            else:
                                db.update_trader_state(
                                    addr,
                                    status="ROTATED_OUT",
                                    consecutive_losses=consec_losses,
                                    copied_wins=copied_wins,
                                    copied_losses=copied_losses,
                                    latest_tid=max(newest_tid, ev["tid"]),
                                    open_positions=open_pos,
                                    recent_closed_trades=closed_events[:12],
                                )
                            whale_rotated = True
                            break
                    elif ev["closed_pnl"] > 0:
                        consec_losses = 0
                        copied_wins += 1

            if whale_rotated:
                continue

            effective_tid = max(last_tid, newest_tid)
            db.update_trader_state(
                addr,
                consecutive_losses=consec_losses,
                copied_wins=copied_wins,
                copied_losses=copied_losses,
                last_fill_tid=effective_tid,
                open_positions=open_pos,
                recent_closed_trades=closed_events[:12],
            )

            # 2. Detect closed positions that were previously signaled -> send Exit Alert & unlock re-entry!
            current_open_keys = {f"{addr}:{p['coin']}:{p['direction']}" for p in open_pos}
            prev_signaled = db.get_copy_signaled_positions_for_trader(addr)
            for ps in prev_signaled:
                if ps["pos_key"] not in current_open_keys:
                    db.delete_copy_signaled_position(ps["pos_key"])
                    if auto_emit_new_signals and settings.get("TELEGRAM_AUTO_SEND", True):
                        match_ev = next(
                            (e for e in closed_events[:5] if str(e.get("coin", "")).upper() == str(ps["coin"]).upper()),
                            None,
                        )
                        pnl_txt = (
                            f" | نتیجه نهنگ: <code>${match_ev['closed_pnl']:+,.2f}</code>"
                            if match_ev and "closed_pnl" in match_ev
                            else ""
                        )
                        exit_msg = (
                            f"🔔🐋 <b>هشدار خروج کپی‌تریدر | بستن پوزیشن #{ps['coin']}USDT</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"👤 تریدر مرجع <code>{whale['label']}</code> پوزیشن <b>{ps['direction']}</b> روی <b>#{ps['coin']}</b> "
                            f"(ورود <code>${self.fmt_px(float(ps['entry_price']))}</code>) را بست{pnl_txt}.\n"
                            f"💡 در صورت همراهی با این سیگنال، سیو سود / بستن پوزیشن بررسی شود."
                        )
                        await telegram_agent.send_html_message(
                            exit_msg, msg_type="COPY_EXIT", symbol=ps["coin"]
                        )

            # 3. Check open positions & dispatch standalone Copy-Trade signals for every newly discovered position
            if auto_emit_new_signals and open_pos:
                refreshed_whale = next((t for t in db.get_traders() if t["address"] == addr), whale)
                for pos in open_pos[:4]:
                    val_usd = float(pos.get("position_value_usd", 0.0))
                    roe_pct = abs(float(pos.get("roe_pct", 0.0)))
                    if val_usd < 8000:
                        continue
                    pos_key = f"{addr}:{pos['coin']}:{pos['direction']}"
                    prev_rec = db.get_copy_signaled_position(pos_key)
                    cur_entry = float(pos.get("entry_price") or 0.0)
                    entry_shifted = (
                        abs(cur_entry - float(prev_rec["entry_price"])) / max(float(prev_rec["entry_price"]), 1e-9) >= 0.015
                        if prev_rec and float(prev_rec.get("entry_price") or 0) > 0
                        else False
                    )

                    if (not prev_rec or entry_shifted) and roe_pct <= 95.0:
                        sig_dict = await self.build_copy_signal(refreshed_whale, pos)
                        db.save_signal(sig_dict)
                        if settings.get("TELEGRAM_AUTO_SEND", True):
                            tg_res = await telegram_agent.send_signal(sig_dict)
                            if tg_res.get("ok"):
                                sig_dict["telegram_sent"] = True
                                db.mark_copy_position_signaled(
                                    pos_key, addr, pos["coin"], pos["direction"], cur_entry
                                )
                        else:
                            db.mark_copy_position_signaled(
                                pos_key, addr, pos["coin"], pos["direction"], cur_entry
                            )
                        emitted.append(sig_dict)

        updated_active = db.get_active_trader() or active
        return {
            "status": "ok",
            "active": updated_active,
            "rotated": rotated_any,
            "rotation_event": rotation_event,
            "emitted_signals": emitted,
        }

    async def emit_copy_signals_now(
        self, coin: Optional[str] = None, send_to_telegram: bool = True
    ) -> Dict[str, Any]:
        """On-demand generation & Telegram delivery of standalone Copy-Trade signals from Active Trader."""
        await self.monitor_active_trader(auto_emit_new_signals=False)
        active = db.get_active_trader()
        if not active:
            return {"ok": False, "error": "هیچ تریدر فعالی در لیست وجود ندارد."}

        positions = active.get("open_positions") or []
        if coin:
            positions = [p for p in positions if str(p.get("coin", "")).upper() == coin.upper()]

        # If active #1 trader currently has 0 open positions and no specific coin filter, check if another standby whale has open positions or inform clearly
        if not positions:
            return {
                "ok": False,
                "active_trader": active,
                "error": f"تریدر فعال فعلی ({active['label']}) در این لحظه پوزیشن فیوچرز بازی ندارد (به محض باز کردن معامله جدید، سیگنال کپی مستقل به تلگرام ارسال می‌شود).",
            }

        sent_signals = []
        last_tg_res = None
        for pos in positions[:3]:
            sig_dict = await self.build_copy_signal(active, pos)
            db.save_signal(sig_dict)
            pos_key = f"{active['address']}:{pos['coin']}:{pos['direction']}"
            db.mark_copy_position_signaled(
                pos_key, active["address"], pos["coin"], pos["direction"], pos["entry_price"]
            )
            if send_to_telegram:
                last_tg_res = await telegram_agent.send_signal(sig_dict)
                if last_tg_res.get("ok"):
                    sig_dict["telegram_sent"] = True
            sent_signals.append(sig_dict)

        return {
            "ok": True,
            "active_trader": active,
            "signals": sent_signals,
            "telegram_result": last_tg_res,
        }

    async def record_trade_outcome(self, outcome: str, note_fa: str = "") -> Dict[str, Any]:
        active = db.get_active_trader()
        if not active:
            await self.refresh_leaderboard_roster(force_reseed=True)
            active = db.get_active_trader()
        if not active:
            return {"ok": False, "error": "هیچ تریدر فعالی در لیست وجود ندارد."}

        max_losses = int(settings.get("COPY_TRADER_MAX_CONSECUTIVE_LOSSES", 2))
        consec = int(active.get("consecutive_losses", 0))
        wins = int(active.get("copied_wins", 0))
        losses = int(active.get("copied_losses", 0))

        is_win = outcome.upper() == "WIN"
        if is_win:
            consec = 0
            wins += 1
            db.update_trader_state(
                active["address"],
                consecutive_losses=0,
                copied_wins=wins,
            )
            return {
                "ok": True,
                "rotated": False,
                "message_fa": f"برد ثبت شد! شمارنده خطای متوالی {active['label']} به 0/{max_losses} ریست شد.",
                "active_trader": db.get_active_trader(),
            }

        consec += 1
        losses += 1
        if consec >= max_losses:
            reason = note_fa or f"ثبت {consec} معامله ناموفق متوالی در کپی‌تریدینگ"
            rot = await self._rotate_active_trader(
                old_trader=active,
                consec_losses=consec,
                copied_wins=wins,
                copied_losses=losses,
                latest_tid=int(active.get("last_fill_tid", 0)),
                open_pos=active.get("open_positions", []),
                closed_events=active.get("recent_closed_trades", []),
                reason_fa=reason,
            )
            new_lbl = rot["new_active"]["label"] if rot.get("new_active") else "نامشخص"
            return {
                "ok": True,
                "rotated": True,
                "message_fa": f"خطای متوالی به {consec}/{max_losses} رسید! تریدر {active['label']} کنار گذاشته شد و {new_lbl} جایگزین گردید.",
                "rotation": rot,
                "active_trader": rot.get("new_active"),
            }

        db.update_trader_state(
            active["address"],
            consecutive_losses=consec,
            copied_losses=losses,
        )
        return {
            "ok": True,
            "rotated": False,
            "message_fa": f"هشدار: اولین خطا ثبت شد ({consec}/{max_losses}). در صورت یک خطای دیگر، تریدر به صورت خودکار تعویض می‌شود.",
            "active_trader": db.get_active_trader(),
        }

    async def _rotate_active_trader(
        self,
        *,
        old_trader: Dict[str, Any],
        consec_losses: int,
        copied_wins: int,
        copied_losses: int,
        latest_tid: int,
        open_pos: List[Dict[str, Any]],
        closed_events: List[Dict[str, Any]],
        reason_fa: str,
    ) -> Dict[str, Any]:
        db.update_trader_state(
            old_trader["address"],
            status="ROTATED_OUT",
            consecutive_losses=consec_losses,
            copied_wins=copied_wins,
            copied_losses=copied_losses,
            last_fill_tid=latest_tid,
            open_positions=open_pos,
            recent_closed_trades=closed_events,
        )

        all_traders = db.get_traders()
        standby = [t for t in all_traders if t["status"] == "STANDBY" and t["address"] != old_trader["address"]]

        if not standby:
            fresh = await hl_copy_source.fetch_leaderboard_candidates(
                top_n=int(settings.get("COPY_TRADER_ROSTER_SIZE", 12))
            )
            for c in fresh:
                if c["address"] != old_trader["address"]:
                    c["status"] = "STANDBY"
                    c["_force_state"] = True
                    c["consecutive_losses"] = 0
                    db.upsert_trader(c)
            all_traders = db.get_traders()
            standby = [t for t in all_traders if t["status"] == "STANDBY" and t["address"] != old_trader["address"]]

        new_active: Optional[Dict[str, Any]] = None
        if standby:
            # Prefer standby trader with open positions if available, else #1 standby
            chosen = next((s for s in standby if s.get("open_positions")), standby[0])
            try:
                pos, cl_ev, _, new_tid = await hl_copy_source.fetch_trader_state_and_fills(chosen["address"])
            except Exception:
                pos, cl_ev, new_tid = chosen.get("open_positions", []), chosen.get("recent_closed_trades", []), chosen.get("last_fill_tid", 0)

            db.update_trader_state(
                chosen["address"],
                status="ACTIVE",
                consecutive_losses=0,
                last_fill_tid=new_tid,
                open_positions=pos,
                recent_closed_trades=cl_ev[:12],
            )
            new_active = db.get_active_trader()

        ts = _now_iso()
        db.log_rotation(
            timestamp_iso=ts,
            old_address=old_trader["address"],
            old_label=old_trader["label"],
            reason_fa=reason_fa,
            consecutive_losses=consec_losses,
            new_address=new_active["address"] if new_active else None,
            new_label=new_active["label"] if new_active else None,
        )

        if settings.get("TELEGRAM_AUTO_SEND", True):
            msg = telegram_agent.format_rotation_message(
                old_label=old_trader["label"],
                old_addr=old_trader["address"],
                losses=consec_losses,
                new_label=new_active["label"] if new_active else None,
                new_addr=new_active["address"] if new_active else None,
                new_roi=float(new_active.get("roi_month_pct", 0.0)) if new_active else 0.0,
                new_winrate=float(new_active.get("win_rate_pct", 0.0)) if new_active else 0.0,
                reason_fa=reason_fa,
            )
            await telegram_agent.send_html_message(msg, msg_type="TRADER_ROTATION")

        return {
            "timestamp_iso": ts,
            "old_trader": old_trader["label"],
            "new_active": new_active,
            "reason_fa": reason_fa,
        }

    async def activate_specific_trader(self, address: str) -> Optional[Dict[str, Any]]:
        traders = db.get_traders()
        for t in traders:
            if t["status"] == "ACTIVE" and t["address"] != address:
                db.update_trader_state(t["address"], status="STANDBY")
        db.update_trader_state(address, status="ACTIVE", consecutive_losses=0)
        await self.monitor_active_trader(auto_emit_new_signals=False)
        return db.get_active_trader()

    async def analyze(self, symbol: str, context: Dict[str, Any]) -> AgentReport:
        """Standalone summary report if queried directly."""
        active = db.get_active_trader()
        return AgentReport(
            agent_id=self.agent_id,
            agent_name_fa=self.agent_name_fa,
            category_fa=self.category_fa,
            symbol=symbol,
            score=0.0,
            direction="NEUTRAL",
            confidence=75.0,
            summary_fa="موتور کپی‌تریدر به صورت کاملاً مستقل فعال است و سیگنال‌های خود را جداگانه ارسال می‌کند.",
            bullets_fa=[],
            metrics={"active_trader_label": active["label"] if active else None},
        )


copy_trader_agent = CopyTraderAgent()
