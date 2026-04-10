#!/usr/bin/env python3
"""
NOSTRADAM — Phase 1: Paper Trading MVP
BTC 5-min prediction market inefficiency scanner for Polymarket.

Run: python main.py
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime, timezone

from core.config import load_config
from core.database import get_db, init_db, log_market, log_snapshot, get_snapshots_for_market
from core.scanner import MarketScanner
from core.analyzer import Analyzer
from core.trader import PaperTrader
from dashboard.app import create_app

# ── Logging ──────────────────────────────────────────────
def setup_logging(level):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s │ %(name)-20s │ %(levelname)-5s │ %(message)s",
        datefmt="%H:%M:%S",
    )

# ── Bot Loop ─────────────────────────────────────────────
def bot_loop(cfg, conn, scanner, analyzer, trader):
    log = logging.getLogger("nostradam.main")
    cycle = cfg["cycle_interval_seconds"]
    active_markets = {}  # market_id -> {token_ids, snapshots_count, ...}

    log.info("=" * 60)
    log.info("  NOSTRADAM v0.1 — Paper Trading Mode")
    log.info(f"  Bankroll: ${cfg['bankroll']} | Min edge: {cfg['strategy']['min_edge']*100}%")
    log.info(f"  Cycle: every {cycle}s | Dashboard: :{cfg['dashboard']['port']}")
    log.info("=" * 60)

    while True:
        try:
            # ── Step 1: Discover markets ──
            markets = scanner.fetch_btc_minute_markets()

            for market in markets:
                mid = market["id"]
                if not mid:
                    continue

                log_market(conn, market)

                token_ids = market.get("token_ids", [])
                if len(token_ids) < 1:
                    continue

                # ── Step 2: Fetch order book ──
                yes_token = token_ids[0] if token_ids else None
                no_token = token_ids[1] if len(token_ids) > 1 else None

                book_yes = scanner.get_order_book(yes_token) if yes_token else None
                book_no = scanner.get_order_book(no_token) if no_token else None

                parsed_yes = scanner.parse_book(book_yes) if book_yes else None
                parsed_no = scanner.parse_book(book_no) if book_no else None

                if not parsed_yes:
                    continue

                # ── Step 3: Log snapshot ──
                snap = {
                    "best_bid_yes": parsed_yes["best_bid"],
                    "best_ask_yes": parsed_yes["best_ask"],
                    "best_bid_no": parsed_no["best_bid"] if parsed_no else 0,
                    "best_ask_no": parsed_no["best_ask"] if parsed_no else 0,
                    "spread_yes": parsed_yes["spread"],
                    "spread_no": parsed_no["spread"] if parsed_no else 0,
                    "mid_yes": parsed_yes["mid"],
                    "volume": parsed_yes.get("depth", 0),
                    "book_depth_yes": parsed_yes["depth"],
                    "book_depth_no": parsed_no["depth"] if parsed_no else 0,
                }
                log_snapshot(conn, mid, snap)

                # Track this market
                if mid not in active_markets:
                    active_markets[mid] = {"seen": 0}
                active_markets[mid]["seen"] += 1

                # ── Step 4: Analyze (need at least 3 snapshots) ──
                snapshots_raw = get_snapshots_for_market(conn, mid)
                snapshots = [dict(s) for s in snapshots_raw]

                if len(snapshots) < 3:
                    log.debug(f"Market {mid[:12]}... — {len(snapshots)} snapshots (need 3+)")
                    continue

                signals = analyzer.analyze(snapshots, parsed_yes, parsed_no)

                # ── Step 5: Trade if signal found ──
                for signal in signals:
                    signal.meta["current_mid"] = parsed_yes["mid"]
                    trader.execute(signal, mid)

            # ── Step 6: Check for resolved markets ──
            # In a real setup, we'd poll Polymarket for resolution.
            # For paper trading, we resolve based on end_time passing.
            _check_resolutions(conn, scanner, trader, log)

            # ── Cleanup old market tracking ──
            active_markets = {k: v for k, v in active_markets.items() if v["seen"] < 100}

        except KeyboardInterrupt:
            log.info("Shutting down...")
            break
        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)

        time.sleep(cycle)


def _check_resolutions(conn, scanner, trader, log):
    """Check if any markets with open trades have resolved."""
    from core.database import get_open_trades
    open_trades = get_open_trades(conn)

    for trade in open_trades:
        mid = trade["market_id"]
        # Check market end time
        row = conn.execute("SELECT end_time, resolved, outcome FROM markets WHERE id=?", (mid,)).fetchone()
        if not row:
            continue

        end_time = row["end_time"]
        if not end_time:
            continue

        try:
            end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        now = datetime.now(timezone.utc)
        if now < end_dt:
            continue  # Market not yet ended

        # Market ended — try to fetch resolution from Polymarket
        if row["resolved"]:
            outcome = row["outcome"]
        else:
            # Try to get actual resolution
            outcome = _fetch_resolution(scanner, mid)
            if outcome:
                conn.execute("UPDATE markets SET resolved=1, outcome=? WHERE id=?", (outcome, mid))
                conn.commit()
            else:
                # If we can't get resolution after 2 minutes past end, skip
                if (now - end_dt).total_seconds() > 120:
                    log.warning(f"Could not resolve market {mid[:12]}... — marking as NO by default")
                    outcome = "NO"  # conservative default
                    conn.execute("UPDATE markets SET resolved=1, outcome=? WHERE id=?", (outcome, mid))
                    conn.commit()
                else:
                    continue

        if outcome:
            trader.resolve_market(mid, outcome)
            log.info(f"Market {mid[:12]}... resolved: {outcome}")


def _fetch_resolution(scanner, market_id):
    """Try to fetch market resolution from Polymarket."""
    try:
        import requests
        resp = requests.get(
            f"https://gamma-api.polymarket.com/markets/{market_id}",
            timeout=10
        )
        if resp.ok:
            data = resp.json()
            if data.get("resolved"):
                # outcomePrices: "[1, 0]" means YES won, "[0, 1]" means NO won
                prices = data.get("outcomePrices", "")
                if isinstance(prices, str):
                    prices = prices.strip("[]").split(",")
                    if len(prices) >= 2:
                        if float(prices[0].strip()) > 0.5:
                            return "YES"
                        else:
                            return "NO"
    except Exception:
        pass
    return None


# ── Entry Point ──────────────────────────────────────────
def main():
    cfg = load_config()
    setup_logging(cfg.get("log_level", "INFO"))
    log = logging.getLogger("nostradam.main")

    if not cfg["paper_trade"]:
        log.error("Live trading not implemented in Phase 1. Set paper_trade: true")
        sys.exit(1)

    # Database
    conn = get_db(cfg.get("db_path", "nostradam.db"))
    init_db(conn)

    # Components
    scanner = MarketScanner(cfg)
    analyzer = Analyzer(cfg)
    trader = PaperTrader(cfg, conn)

    # Dashboard in background thread
    if cfg["dashboard"]["enabled"]:
        app = create_app(conn, trader)
        dash_thread = threading.Thread(
            target=lambda: app.run(
                host=cfg["dashboard"]["host"],
                port=cfg["dashboard"]["port"],
                debug=False,
                use_reloader=False,
            ),
            daemon=True,
        )
        dash_thread.start()
        log.info(f"Dashboard running at http://localhost:{cfg['dashboard']['port']}")

    # Run bot
    bot_loop(cfg, conn, scanner, analyzer, trader)


if __name__ == "__main__":
    main()
