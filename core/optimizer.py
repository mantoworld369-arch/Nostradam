"""Optimizer: Runs between sessions to analyze and adjust strategy."""

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
        log.info(f"{'='*50}")
        log.info(f"  OPTIMIZATION — Session {session_id}")
        log.info(f"{'='*50}")

        notes = []
        new_cfg = copy.deepcopy(self.cfg)

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
            notes.append("Too few trades to optimize. Keeping settings.")
            log.info("Too few trades — skip optimization")
            db.end_session(self.conn, session_id, "\n".join(notes))
            return new_cfg, "\n".join(notes)

        win_rate = wins / resolved

        # Signal analysis
        notes.append("=== SIGNAL ANALYSIS ===")
        enabled = list(new_cfg["strategy"].get("enabled_signals", [
            "mean_reversion", "book_imbalance", "momentum", "spread_compression", "stale_odds"
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

            if sig_total >= 5 and sig_wr < 0.30 and sig_pnl < 0:
                if sig_type in enabled and len(enabled) > 1:
                    enabled.remove(sig_type)
                    notes.append(f"  -> DISABLED {sig_type}")

        new_cfg["strategy"]["enabled_signals"] = enabled

        # Edge analysis
        notes.append("\n=== EDGE ANALYSIS ===")
        for ep in edge_perf:
            ep_total = ep["total"]
            ep_wins = ep["wins"] or 0
            ep_pnl = ep["pnl"] or 0
            ep_wr = ep_wins / ep_total if ep_total > 0 else 0
            notes.append(f"  {ep['edge_bucket']}: {ep_wins}/{ep_total} ({ep_wr:.0%}) PnL=${ep_pnl:.2f}")

        low_edge = [ep for ep in edge_perf if ep["edge_bucket"] == "low_3-5"]
        if low_edge and low_edge[0]["total"] >= 3 and (low_edge[0]["pnl"] or 0) < 0:
            old = new_cfg["strategy"]["min_edge"]
            new_cfg["strategy"]["min_edge"] = min(old + 0.01, 0.15)
            notes.append(f"  -> Raised min_edge: {old:.2f} -> {new_cfg['strategy']['min_edge']:.2f}")

        # Sizing
        notes.append("\n=== SIZING ===")
        if win_rate > 0.55 and pnl > 0:
            old = new_cfg["max_bet_pct"]
            new_cfg["max_bet_pct"] = min(old * 1.1, 0.10)
            notes.append(f"  -> Increased: {old:.1%} -> {new_cfg['max_bet_pct']:.1%}")
        elif win_rate < 0.35 and pnl < 0:
            old = new_cfg["max_bet_pct"]
            new_cfg["max_bet_pct"] = max(old * 0.8, 0.02)
            notes.append(f"  -> Decreased: {old:.1%} -> {new_cfg['max_bet_pct']:.1%}")
        else:
            notes.append("  -> Unchanged")

        # Save snapshot
        snapshot_path = os.path.join(self.history_dir, f"session_{session_id:04d}.json")
        with open(snapshot_path, "w") as f:
            json.dump({
                "session_id": session_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "performance": {"total": total, "wins": wins, "losses": losses, "pnl": pnl, "win_rate": round(win_rate * 100, 1)},
                "signal_performance": signal_perf,
                "config_after": {k: v for k, v in new_cfg.items() if k != "api"},
                "notes": notes,
            }, f, indent=2, default=str)

        optimization_summary = "\n".join(notes)
        db.end_session(self.conn, session_id, optimization_summary)
        return new_cfg, optimization_summary
