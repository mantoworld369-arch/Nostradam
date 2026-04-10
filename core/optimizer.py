"""
Optimizer: Runs between sessions to analyze performance and adjust strategy.

After each 30-min session:
1. Analyze which signal types won/lost
2. Which edge ranges were profitable
3. Which sides (YES/NO) performed
4. Adjust min_edge, enabled signals, position sizing
5. Save session config snapshot
"""

import logging
import json
import copy
import os
from datetime import datetime, timezone
from core import database as db

log = logging.getLogger("nostradam.optimizer")


class Optimizer:
    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.conn = conn
        self.history_dir = "session_history"
        os.makedirs(self.history_dir, exist_ok=True)

    def optimize(self, session_id):
        """
        Analyze session results and return optimized config + notes.
        """
        log.info(f"{'='*50}")
        log.info(f"  OPTIMIZATION — Analyzing Session {session_id}")
        log.info(f"{'='*50}")

        notes = []
        new_cfg = copy.deepcopy(self.cfg)

        # Get session data
        perf = db.get_session_performance(self.conn, session_id)
        signal_perf = db.get_signal_performance(self.conn, session_id)
        edge_perf = db.get_edge_range_performance(self.conn, session_id)
        side_perf = db.get_side_performance(self.conn, session_id)

        total = perf.get("total", 0)
        wins = perf.get("wins", 0) or 0
        losses = perf.get("losses", 0) or 0
        resolved = wins + losses
        pnl = perf.get("total_pnl", 0) or 0

        log.info(f"Session {session_id}: {total} trades, {wins}W/{losses}L, PnL=${pnl:.2f}")

        if resolved < 3:
            notes.append("Too few resolved trades to optimize. Keeping current settings.")
            log.info("Too few trades — skipping optimization")
            return new_cfg, "\n".join(notes)

        win_rate = wins / resolved if resolved > 0 else 0

        # --- 1. Optimize signal types ---
        notes.append("=== SIGNAL ANALYSIS ===")
        enabled = list(new_cfg["strategy"].get("enabled_signals", [
            "mean_reversion", "book_imbalance", "momentum",
            "spread_compression", "stale_odds"
        ]))

        for sp in signal_perf:
            sig_type = sp["signal_type"]
            sig_total = sp["total"]
            sig_wins = sp["wins"] or 0
            sig_pnl = sp["pnl"] or 0
            sig_wr = sig_wins / sig_total if sig_total > 0 else 0

            report = f"  {sig_type}: {sig_wins}/{sig_total} ({sig_wr:.0%}) PnL=${sig_pnl:.2f}"
            log.info(report)
            notes.append(report)

            # Disable signals with >5 trades and <30% win rate and negative PnL
            if sig_total >= 5 and sig_wr < 0.30 and sig_pnl < 0:
                if sig_type in enabled and len(enabled) > 1:
                    enabled.remove(sig_type)
                    note = f"  -> DISABLED {sig_type} (poor performance)"
                    log.info(note)
                    notes.append(note)

            # Re-enable previously disabled signals every 5 sessions to re-test
            # (will be done via session count check in main)

        new_cfg["strategy"]["enabled_signals"] = enabled

        # --- 2. Optimize edge threshold ---
        notes.append("\n=== EDGE ANALYSIS ===")
        for ep in edge_perf:
            bucket = ep["edge_bucket"]
            ep_total = ep["total"]
            ep_wins = ep["wins"] or 0
            ep_pnl = ep["pnl"] or 0
            ep_wr = ep_wins / ep_total if ep_total > 0 else 0

            report = f"  {bucket}: {ep_wins}/{ep_total} ({ep_wr:.0%}) PnL=${ep_pnl:.2f}"
            log.info(report)
            notes.append(report)

        # If low-edge trades are losing money, raise the threshold
        low_edge = [ep for ep in edge_perf if ep["edge_bucket"] == "low_3-5"]
        if low_edge:
            le = low_edge[0]
            le_total = le["total"]
            le_wins = le["wins"] or 0
            le_pnl = le["pnl"] or 0
            if le_total >= 3 and le_pnl < 0:
                old_edge = new_cfg["strategy"]["min_edge"]
                new_cfg["strategy"]["min_edge"] = min(old_edge + 0.01, 0.15)
                note = f"  -> Raised min_edge: {old_edge:.2f} -> {new_cfg['strategy']['min_edge']:.2f}"
                log.info(note)
                notes.append(note)

        # If high-edge trades are winning, lower threshold to catch more
        high_edge = [ep for ep in edge_perf if ep["edge_bucket"] in ("high_10-20", "extreme_20+")]
        if high_edge:
            total_high_wins = sum(ep.get("wins", 0) or 0 for ep in high_edge)
            total_high = sum(ep["total"] for ep in high_edge)
            if total_high >= 3 and total_high_wins / total_high > 0.6:
                note = f"  -> High-edge trades performing well ({total_high_wins}/{total_high})"
                log.info(note)
                notes.append(note)

        # --- 3. Optimize by side ---
        notes.append("\n=== SIDE ANALYSIS ===")
        for sp in side_perf:
            side = sp["side"]
            sp_total = sp["total"]
            sp_wins = sp["wins"] or 0
            sp_pnl = sp["pnl"] or 0
            sp_wr = sp_wins / sp_total if sp_total > 0 else 0

            report = f"  {side}: {sp_wins}/{sp_total} ({sp_wr:.0%}) PnL=${sp_pnl:.2f}"
            log.info(report)
            notes.append(report)

        # --- 4. Adjust position sizing ---
        notes.append("\n=== SIZING ADJUSTMENT ===")
        if win_rate > 0.55 and pnl > 0:
            old_pct = new_cfg["max_bet_pct"]
            new_cfg["max_bet_pct"] = min(old_pct * 1.1, 0.10)  # cap at 10%
            note = f"  -> Increased bet size: {old_pct:.1%} -> {new_cfg['max_bet_pct']:.1%} (winning session)"
            log.info(note)
            notes.append(note)
        elif win_rate < 0.35 and pnl < 0:
            old_pct = new_cfg["max_bet_pct"]
            new_cfg["max_bet_pct"] = max(old_pct * 0.8, 0.02)  # floor at 2%
            note = f"  -> Decreased bet size: {old_pct:.1%} -> {new_cfg['max_bet_pct']:.1%} (losing session)"
            log.info(note)
            notes.append(note)
        else:
            notes.append("  -> Sizing unchanged")

        # --- 5. Save session snapshot ---
        snapshot_path = os.path.join(self.history_dir, f"session_{session_id:04d}.json")
        snapshot = {
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "performance": {
                "total": total, "wins": wins, "losses": losses,
                "pnl": pnl, "win_rate": round(win_rate * 100, 1),
            },
            "signal_performance": signal_perf,
            "edge_performance": edge_perf,
            "side_performance": side_perf,
            "config_before": {k: v for k, v in self.cfg.items() if k != "api"},
            "config_after": {k: v for k, v in new_cfg.items() if k != "api"},
            "optimization_notes": notes,
        }

        with open(snapshot_path, "w") as f:
            json.dump(snapshot, f, indent=2, default=str)

        log.info(f"Session snapshot saved: {snapshot_path}")
        log.info(f"{'='*50}")

        optimization_summary = "\n".join(notes)
        db.end_session(self.conn, session_id, optimization_summary)

        return new_cfg, optimization_summary
