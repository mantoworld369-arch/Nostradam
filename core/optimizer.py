"""Optimizer: Analyze sessions and adjust strategy."""
import logging, json, copy, os
from datetime import datetime, timezone
from core import database as db

log = logging.getLogger("nostradam.optimizer")

class Optimizer:
    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.conn = conn
        os.makedirs("session_history", exist_ok=True)

    def optimize(self, session_id):
        log.info(f"{'='*50}\n  OPTIMIZATION — Session {session_id}\n{'='*50}")
        notes = []
        new_cfg = copy.deepcopy(self.cfg)
        perf = db.get_session_performance(self.conn, session_id)
        signal_perf = db.get_signal_performance(self.conn, session_id)
        edge_perf = db.get_edge_range_performance(self.conn, session_id)

        total = perf.get("total", 0)
        wins = perf.get("wins", 0) or 0
        losses = perf.get("losses", 0) or 0
        resolved = wins + losses
        pnl = perf.get("total_pnl", 0) or 0

        if resolved < 3:
            notes.append("Too few trades to optimize.")
            db.end_session(self.conn, session_id, "\n".join(notes))
            return new_cfg, "\n".join(notes)

        win_rate = wins / resolved

        enabled = list(new_cfg["strategy"].get("enabled_signals", [
            "mean_reversion", "book_imbalance", "momentum", "spread_compression", "stale_odds"]))

        for sp in signal_perf:
            st, stot, sw, spnl = sp["signal_type"], sp["total"], sp["wins"] or 0, sp["pnl"] or 0
            swr = sw / stot if stot > 0 else 0
            notes.append(f"  {st}: {sw}/{stot} ({swr:.0%}) ${spnl:.2f}")
            if stot >= 5 and swr < 0.30 and spnl < 0 and st in enabled and len(enabled) > 1:
                enabled.remove(st)
                notes.append(f"  -> DISABLED {st}")

        new_cfg["strategy"]["enabled_signals"] = enabled

        low = [e for e in edge_perf if e["edge_bucket"] == "low_3-5"]
        if low and low[0]["total"] >= 3 and (low[0]["pnl"] or 0) < 0:
            old = new_cfg["strategy"]["min_edge"]
            new_cfg["strategy"]["min_edge"] = min(old + 0.01, 0.15)

        if win_rate > 0.55 and pnl > 0:
            new_cfg["max_bet_pct"] = min(new_cfg["max_bet_pct"] * 1.1, 0.10)
        elif win_rate < 0.35 and pnl < 0:
            new_cfg["max_bet_pct"] = max(new_cfg["max_bet_pct"] * 0.8, 0.02)

        with open(f"session_history/session_{session_id:04d}.json", "w") as f:
            json.dump({"session_id": session_id, "performance": {"total": total, "wins": wins, "losses": losses, "pnl": pnl},
                       "notes": notes}, f, indent=2, default=str)

        db.end_session(self.conn, session_id, "\n".join(notes))
        return new_cfg, "\n".join(notes)
