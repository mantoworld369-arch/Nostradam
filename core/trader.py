"""
Trader: Paper trade execution with realistic pricing.
- Entries use ASK price (what you'd actually pay), not mid
- Tracks unrealized P&L on open positions
- Session-aware
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
        self.session_id = 0

    def set_session(self, session_id):
        self.session_id = session_id

    def execute(self, signal, market_id, book_yes=None, book_no=None):
        """Place a paper trade using ACTUAL ask price."""
        if not self._risk_check():
            log.warning("Risk check failed — skipping trade")
            return None

        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            log.info(f"Cooldown: {self.cooldown_remaining} markets remaining")
            return None

        open_trades = db.get_open_trades(self.conn)
        if len(open_trades) >= self.cfg["risk"]["max_open_positions"]:
            log.info("Max open positions reached")
            return None

        # Determine entry price from the ORDER BOOK (ask side = what you pay)
        mid_price = signal.meta.get("current_mid", 0.5)

        if signal.side == "YES" and book_yes:
            entry_ask = book_yes.get("best_ask", mid_price)
            entry_spread = book_yes.get("spread", 0)
        elif signal.side == "NO" and book_no:
            entry_ask = book_no.get("best_ask", 1 - mid_price)
            entry_spread = book_no.get("spread", 0)
        else:
            # Fallback: estimate ask from mid + half spread
            entry_ask = mid_price + 0.02 if signal.side == "YES" else (1 - mid_price) + 0.02
            entry_spread = 0.04

        entry_mid = mid_price if signal.side == "YES" else (1 - mid_price)

        # Sanity check: don't enter at extreme prices
        if entry_ask < 0.02 or entry_ask > 0.98:
            log.info(f"Entry price {entry_ask:.3f} too extreme — skipping")
            return None

        # Recalculate edge based on ask (not mid)
        # Edge = how much we think fair value differs from what we'd pay
        if signal.side == "YES":
            fair_value = mid_price + signal.edge
            real_edge = fair_value - entry_ask
        else:
            fair_value = (1 - mid_price) + signal.edge
            real_edge = fair_value - entry_ask

        if real_edge < self.cfg["strategy"]["min_edge"] * 0.5:
            log.info(f"Edge after spread too low ({real_edge:.3f}) — skipping")
            return None

        size = self._size_position(signal, real_edge)
        if size < self.cfg["min_bet"]:
            log.info(f"Size too small ({size:.2f}) — skipping")
            return None

        trade = {
            "market_id": market_id,
            "session_id": self.session_id,
            "side": signal.side,
            "entry_price": entry_ask,
            "entry_mid": entry_mid,
            "entry_ask": entry_ask,
            "entry_spread": entry_spread,
            "size": size,
            "edge": real_edge,
            "signal_type": signal.signal_type,
            "meta": signal.meta,
        }

        db.log_trade(self.conn, trade)
        self.bankroll -= size

        log.info(f"PAPER TRADE: {signal.side} on {market_id[:12]}... "
                 f"size=${size:.2f} @ ask={entry_ask:.3f} (mid={entry_mid:.3f}) "
                 f"edge={real_edge:.3f} spread={entry_spread:.3f} [{signal.signal_type}]")

        return trade

    def update_open_positions(self, scanner):
        """Update current price on all open positions for unrealized P&L."""
        open_trades = db.get_open_trades(self.conn)
        for t in open_trades:
            mid = t["market_id"]
            # Fetch current market state
            market = scanner.known_markets.get(mid)
            if not market:
                continue

            token_ids = market.get("token_ids", [])
            if not token_ids:
                continue

            if t["side"] == "YES" and len(token_ids) >= 1:
                book = scanner.get_order_book(token_ids[0])
            elif t["side"] == "NO" and len(token_ids) >= 2:
                book = scanner.get_order_book(token_ids[1])
            else:
                continue

            parsed = scanner.parse_book(book)
            if parsed:
                # Current value = best bid (what you could sell for)
                current_price = parsed["best_bid"]
                db.update_trade_current_price(self.conn, t["id"], current_price)

    def resolve_market(self, market_id, outcome):
        open_trades = db.get_open_trades(self.conn)
        for t in open_trades:
            if t["market_id"] != market_id:
                continue

            won = (t["side"] == outcome)

            if won:
                # Binary payout: you paid entry_price, you get $1
                payout = t["size"] / t["entry_price"] if t["entry_price"] > 0 else 0
                pnl = payout - t["size"]
                self.bankroll += payout
                self.consecutive_losses = 0
            else:
                pnl = -t["size"]
                self.consecutive_losses += 1

            self.daily_pnl += pnl

            if self.consecutive_losses >= 3:
                self.cooldown_remaining = self.cfg["risk"]["cooldown_after_loss"]
                log.warning(f"Loss streak of {self.consecutive_losses} — cooling down")

            if self.bankroll > self.peak_bankroll:
                self.peak_bankroll = self.bankroll

            exit_price = 1.0 if won else 0.0
            db.resolve_trade(self.conn, t["id"], exit_price, pnl, won)

            result = "WIN" if won else "LOSS"
            log.info(f"RESOLVED: {result} | {t['side']} | entry={t['entry_price']:.3f} | "
                     f"pnl=${pnl:+.2f} | bankroll=${self.bankroll:.2f}")

    def _risk_check(self):
        max_daily_loss = self.cfg["bankroll"] * self.cfg["risk"]["max_daily_loss_pct"]
        if self.daily_pnl < -max_daily_loss:
            log.warning(f"Daily loss limit hit: ${self.daily_pnl:.2f}")
            return False

        drawdown = (self.peak_bankroll - self.bankroll) / self.peak_bankroll if self.peak_bankroll > 0 else 0
        if drawdown > self.cfg["risk"]["max_drawdown_pct"]:
            log.warning(f"Max drawdown hit: {drawdown:.1%}")
            return False
        return True

    def _size_position(self, signal, real_edge):
        base = self.bankroll * self.cfg["max_bet_pct"]
        edge_mult = min(real_edge / max(self.cfg["strategy"]["min_edge"], 0.01), 2.0)
        conf_mult = signal.confidence
        size = base * edge_mult * conf_mult
        size = max(size, self.cfg["min_bet"])
        size = min(size, self.cfg["max_bet"])
        size = min(size, self.bankroll * 0.1)
        return round(size, 2)

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
            "session_id": self.session_id,
        }
