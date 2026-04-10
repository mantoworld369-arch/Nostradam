"""Trader v0.4: Paper trades with cached prices and proper resolution."""

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
        self.last_known_prices = {}

    def set_session(self, sid):
        self.session_id = sid

    def execute(self, signal, market_id, book_yes=None, book_no=None):
        if not self._risk_check():
            return None
        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            return None
        if len(db.get_open_trades(self.conn)) >= self.cfg["risk"]["max_open_positions"]:
            return None

        mid_price = signal.meta.get("current_mid", 0.5)

        if signal.side == "YES" and book_yes:
            entry_ask = book_yes.get("best_ask", mid_price)
            entry_spread = book_yes.get("spread", 0)
        elif signal.side == "NO" and book_no:
            entry_ask = book_no.get("best_ask", 1 - mid_price)
            entry_spread = book_no.get("spread", 0)
        else:
            entry_ask = mid_price + 0.02 if signal.side == "YES" else (1 - mid_price) + 0.02
            entry_spread = 0.04

        entry_mid = mid_price if signal.side == "YES" else (1 - mid_price)

        if entry_ask < 0.02 or entry_ask > 0.98:
            return None

        real_edge = (mid_price + signal.edge) - entry_ask if signal.side == "YES" else ((1 - mid_price) + signal.edge) - entry_ask
        if real_edge < self.cfg["strategy"]["min_edge"] * 0.5:
            return None

        size = self._size_position(signal, real_edge)
        if size < self.cfg["min_bet"]:
            return None

        trade = {
            "market_id": market_id, "session_id": self.session_id, "side": signal.side,
            "entry_price": entry_ask, "entry_mid": entry_mid, "entry_ask": entry_ask,
            "entry_spread": entry_spread, "size": size, "edge": real_edge,
            "signal_type": signal.signal_type, "meta": signal.meta,
        }
        db.log_trade(self.conn, trade)
        self.bankroll -= size
        log.info(f"TRADE: {signal.side} {market_id[:12]}... ${size:.2f} @{entry_ask:.3f} edge={real_edge:.3f} [{signal.signal_type}]")
        return trade

    def update_open_positions(self, scanner):
        for t in db.get_open_trades(self.conn):
            market = scanner.known_markets.get(t["market_id"])
            if not market:
                continue
            token_ids = market.get("token_ids", [])
            if t["side"] == "YES" and token_ids:
                book = scanner.get_order_book(token_ids[0])
            elif t["side"] == "NO" and len(token_ids) >= 2:
                book = scanner.get_order_book(token_ids[1])
            else:
                continue
            parsed = scanner.parse_book(book)
            if parsed and parsed["best_bid"] > 0:
                self.last_known_prices[t["id"]] = parsed["best_bid"]
                db.update_trade_current_price(self.conn, t["id"], parsed["best_bid"])
            elif t["id"] in self.last_known_prices:
                db.update_trade_current_price(self.conn, t["id"], self.last_known_prices[t["id"]])

    def resolve_market(self, market_id, outcome):
        for t in db.get_open_trades(self.conn):
            if t["market_id"] != market_id:
                continue
            won = (t["side"] == outcome)
            if won:
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
            if self.bankroll > self.peak_bankroll:
                self.peak_bankroll = self.bankroll
            db.resolve_trade(self.conn, t["id"], 1.0 if won else 0.0, pnl, won)
            self.last_known_prices.pop(t["id"], None)
            log.info(f"{'WIN' if won else 'LOSS'} | {t['side']} | @{t['entry_price']:.3f} | ${pnl:+.2f} | bank=${self.bankroll:.2f}")

    def _risk_check(self):
        if self.daily_pnl < -(self.cfg["bankroll"] * self.cfg["risk"]["max_daily_loss_pct"]):
            return False
        dd = (self.peak_bankroll - self.bankroll) / self.peak_bankroll if self.peak_bankroll > 0 else 0
        return dd <= self.cfg["risk"]["max_drawdown_pct"]

    def _size_position(self, signal, real_edge):
        base = self.bankroll * self.cfg["max_bet_pct"]
        mult = min(real_edge / max(self.cfg["strategy"]["min_edge"], 0.01), 2.0)
        size = base * mult * signal.confidence
        return round(min(max(size, self.cfg["min_bet"]), self.cfg["max_bet"], self.bankroll * 0.1), 2)

    def reset_daily(self):
        self.daily_pnl = 0.0

    def get_state(self):
        return {
            "bankroll": round(self.bankroll, 2), "peak": round(self.peak_bankroll, 2),
            "daily_pnl": round(self.daily_pnl, 2), "session_id": self.session_id,
            "drawdown": round((self.peak_bankroll - self.bankroll) / self.peak_bankroll * 100, 2) if self.peak_bankroll > 0 else 0,
            "consecutive_losses": self.consecutive_losses, "cooldown": self.cooldown_remaining,
        }
