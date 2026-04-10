"""
Trader: Paper trade execution, position sizing, risk management.
"""

import logging
from datetime import datetime, timezone
from core import database as db

log = logging.getLogger("nostradam.trader")


class PaperTrader:
    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.conn = conn
        self.bankroll = cfg["bankroll"]
        self.peak_bankroll = self.bankroll
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.cooldown_remaining = 0

    def execute(self, signal, market_id):
        """Place a paper trade based on a signal."""
        # Risk checks
        if not self._risk_check():
            log.warning("Risk check failed — skipping trade")
            return None

        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            log.info(f"Cooldown active — {self.cooldown_remaining} markets remaining")
            return None

        # Check open positions
        open_trades = db.get_open_trades(self.conn)
        if len(open_trades) >= self.cfg["risk"]["max_open_positions"]:
            log.info("Max open positions reached")
            return None

        # Position sizing (simple kelly-inspired)
        size = self._size_position(signal)
        if size < self.cfg["min_bet"]:
            log.info(f"Size too small ({size:.2f}) — skipping")
            return None

        # Entry price = current market price for that side
        entry_price = signal.meta.get("current_mid", 0.5)
        if signal.side == "NO":
            entry_price = 1 - entry_price

        trade = {
            "market_id": market_id,
            "side": signal.side,
            "entry_price": entry_price,
            "size": size,
            "edge": signal.edge,
            "signal_type": signal.signal_type,
            "meta": signal.meta,
        }

        db.log_trade(self.conn, trade)
        self.bankroll -= size  # commit capital

        log.info(f"PAPER TRADE: {signal.side} on {market_id[:12]}... "
                 f"size=${size:.2f} @ {entry_price:.3f} edge={signal.edge:.3f} "
                 f"[{signal.signal_type}]")

        return trade

    def resolve_market(self, market_id, outcome):
        """
        Resolve all open trades for a market.
        outcome: "YES" or "NO" — the actual result.
        """
        open_trades = db.get_open_trades(self.conn)
        for t in open_trades:
            if t["market_id"] != market_id:
                continue

            won = (t["side"] == outcome)

            if won:
                # Payout = size / entry_price (binary option math)
                payout = t["size"] / t["entry_price"] if t["entry_price"] > 0 else 0
                pnl = payout - t["size"]
                self.bankroll += payout
                self.consecutive_losses = 0
            else:
                pnl = -t["size"]
                self.consecutive_losses += 1

            self.daily_pnl += pnl

            # Cooldown after loss streak
            if self.consecutive_losses >= 3:
                self.cooldown_remaining = self.cfg["risk"]["cooldown_after_loss"]
                log.warning(f"Loss streak of {self.consecutive_losses} — cooling down")

            # Track peak for drawdown
            if self.bankroll > self.peak_bankroll:
                self.peak_bankroll = self.bankroll

            exit_price = 1.0 if won else 0.0
            db.resolve_trade(self.conn, t["id"], exit_price, pnl, won)

            result = "WIN" if won else "LOSS"
            log.info(f"RESOLVED: {result} | {t['side']} | pnl=${pnl:+.2f} | "
                     f"bankroll=${self.bankroll:.2f}")

    def _risk_check(self):
        """Check daily loss limit and max drawdown."""
        # Daily loss limit
        max_daily_loss = self.cfg["bankroll"] * self.cfg["risk"]["max_daily_loss_pct"]
        if self.daily_pnl < -max_daily_loss:
            log.warning(f"Daily loss limit hit: ${self.daily_pnl:.2f}")
            return False

        # Max drawdown from peak
        drawdown = (self.peak_bankroll - self.bankroll) / self.peak_bankroll if self.peak_bankroll > 0 else 0
        max_dd = self.cfg["risk"]["max_drawdown_pct"]
        if drawdown > max_dd:
            log.warning(f"Max drawdown hit: {drawdown:.1%}")
            return False

        return True

    def _size_position(self, signal):
        """Calculate position size. Scales with edge and confidence."""
        base = self.bankroll * self.cfg["max_bet_pct"]

        # Scale by edge (more edge = bigger size)
        edge_mult = min(signal.edge / self.min_edge_ref, 2.0)

        # Scale by confidence
        conf_mult = signal.confidence

        size = base * edge_mult * conf_mult
        size = max(size, self.cfg["min_bet"])
        size = min(size, self.cfg["max_bet"])
        size = min(size, self.bankroll * 0.1)  # never more than 10% of remaining

        return round(size, 2)

    @property
    def min_edge_ref(self):
        return max(self.cfg["strategy"]["min_edge"], 0.01)

    def reset_daily(self):
        self.daily_pnl = 0.0

    def get_state(self):
        return {
            "bankroll": round(self.bankroll, 2),
            "peak": round(self.peak_bankroll, 2),
            "daily_pnl": round(self.daily_pnl, 2),
            "drawdown": round((self.peak_bankroll - self.bankroll) / self.peak_bankroll * 100, 2) if self.peak_bankroll > 0 else 0,
            "consecutive_losses": self.consecutive_losses,
            "cooldown": self.cooldown_remaining,
        }
