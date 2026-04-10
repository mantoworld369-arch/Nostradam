#!/usr/bin/env python3
"""NOSTRADAM v0.3 — Self-Optimizing Paper Trading"""

import sys
import time
import logging
import threading
from datetime import datetime, timezone

from core.config import load_config
from core.database import (get_db, init_db, log_market, log_snapshot,
                           get_snapshots_for_market, get_open_trades, start_session)
from core.scanner import MarketScanner
from core.analyzer import Analyzer
from core.trader import PaperTrader
from core.optimizer import Optimizer
from dashboard.app import create_app


def setup_logging(level):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s │ %(name)-20s │ %(levelname)-5s │ %(message)s",
        datefmt="%H:%M:%S",
    )


def trading_loop(cfg, conn, scanner, analyzer, trader, session_duration_sec):
    log = logging.getLogger("nostradam.main")
    cycle = cfg["cycle_interval_seconds"]
    start_time = time.time()

    while time.time() - start_time < session_duration_sec:
        try:
            markets = scanner.fetch_btc_minute_markets()

            for market in markets:
                mid = market["id"]
                if not mid:
                    continue

                log_market(conn, market)
                token_ids = market.get("token_ids", [])
                if len(token_ids) < 1:
                    continue

                yes_token = token_ids[0] if token_ids else None
                no_token = token_ids[1] if len(token_ids) > 1 else None

                book_yes_raw = scanner.get_order_book(yes_token) if yes_token else None
                book_no_raw = scanner.get_order_book(no_token) if no_token else None
                parsed_yes = scanner.parse_book(book_yes_raw) if book_yes_raw else None
                parsed_no = scanner.parse_book(book_no_raw) if book_no_raw else None

                if not parsed_yes:
                    continue

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

                snapshots_raw = get_snapshots_for_market(conn, mid)
                snapshots = [dict(s) for s in snapshots_raw]

                if len(snapshots) < 3:
                    continue

                signals = analyzer.analyze(snapshots, parsed_yes, parsed_no)

                for signal in signals:
                    signal.meta["current_mid"] = parsed_yes["mid"]
                    trader.execute(signal, mid, book_yes=parsed_yes, book_no=parsed_no)

            trader.update_open_positions(scanner)
            check_resolutions(conn, scanner, trader, log)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)

        time.sleep(cycle)


def check_resolutions(conn, scanner, trader, log):
    """Check resolutions — poll immediately when market ends, no 2-min delay."""
    open_trades = get_open_trades(conn)

    for trade in open_trades:
        mid = trade["market_id"]
        row = conn.execute("SELECT end_time, resolved, outcome FROM markets WHERE id=?", (mid,)).fetchone()
        if not row or not row["end_time"]:
            continue

        try:
            end_dt = datetime.fromisoformat(row["end_time"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        now = datetime.now(timezone.utc)
        if now < end_dt:
            continue

        if row["resolved"]:
            outcome = row["outcome"]
        else:
            # Try to fetch immediately
            outcome = fetch_resolution(mid)
            if outcome:
                conn.execute("UPDATE markets SET resolved=1, outcome=? WHERE id=?", (outcome, mid))
                conn.commit()
            elif (now - end_dt).total_seconds() > 60:
                # Fallback after 60s (reduced from 120)
                log.warning(f"Resolution timeout {mid[:12]}... — defaulting NO")
                outcome = "NO"
                conn.execute("UPDATE markets SET resolved=1, outcome=? WHERE id=?", (outcome, mid))
                conn.commit()
            else:
                continue

        if outcome:
            trader.resolve_market(mid, outcome)


def fetch_resolution(market_id):
    try:
        import requests
        resp = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=10)
        if resp.ok:
            data = resp.json()
            if data.get("resolved"):
                prices = data.get("outcomePrices", "")
                if isinstance(prices, str):
                    prices = prices.strip("[]").split(",")
                    if len(prices) >= 2:
                        return "YES" if float(prices[0].strip()) > 0.5 else "NO"
    except Exception:
        pass
    return None


def main():
    cfg = load_config()
    setup_logging(cfg.get("log_level", "INFO"))
    log = logging.getLogger("nostradam.main")

    if not cfg["paper_trade"]:
        log.error("Live trading not implemented. Set paper_trade: true")
        sys.exit(1)

    conn = get_db(cfg.get("db_path", "nostradam.db"))
    init_db(conn)

    scanner = MarketScanner(cfg)
    analyzer = Analyzer(cfg)
    trader = PaperTrader(cfg, conn)
    optimizer = Optimizer(cfg, conn)

    if cfg["dashboard"]["enabled"]:
        app = create_app(conn, trader, scanner)
        threading.Thread(
            target=lambda: app.run(host=cfg["dashboard"]["host"], port=cfg["dashboard"]["port"], debug=False, use_reloader=False),
            daemon=True,
        ).start()
        log.info(f"Dashboard at http://localhost:{cfg['dashboard']['port']}")

    session_minutes = cfg.get("session_duration_minutes", 30)
    session_num = 0

    log.info("=" * 60)
    log.info("  NOSTRADAM v0.3 — Session-Based Paper Trading")
    log.info(f"  Bankroll: ${cfg['bankroll']} | Session: {session_minutes}min")
    log.info("=" * 60)

    while True:
        try:
            session_num += 1
            session_id = start_session(conn, session_minutes,
                                       {k: v for k, v in cfg.items() if k != "api"})
            trader.set_session(session_id)

            log.info(f"\n{'#'*60}")
            log.info(f"  SESSION {session_num} (id={session_id}) — START")
            log.info(f"  Signals: {cfg['strategy'].get('enabled_signals', 'all')}")
            log.info(f"  Min edge: {cfg['strategy']['min_edge']:.2%} | Bet: {cfg['max_bet_pct']:.1%}")
            log.info(f"  Bankroll: ${trader.bankroll:.2f}")
            log.info(f"{'#'*60}\n")

            # ALWAYS wait full session duration
            trading_loop(cfg, conn, scanner, analyzer, trader, session_minutes * 60)

            log.info(f"\n  SESSION {session_num} ENDED — OPTIMIZING...\n")

            # Wait for final resolutions
            time.sleep(15)
            check_resolutions(conn, scanner, trader, log)

            # Optimize
            new_cfg, notes = optimizer.optimize(session_id)
            cfg["strategy"] = new_cfg["strategy"]
            cfg["max_bet_pct"] = new_cfg["max_bet_pct"]
            analyzer.update_params(cfg)

            # Re-enable all signals every 5 sessions
            if session_num % 5 == 0:
                cfg["strategy"]["enabled_signals"] = [
                    "mean_reversion", "book_imbalance", "momentum",
                    "spread_compression", "stale_odds"
                ]
                analyzer.update_params(cfg)
                log.info("Re-enabled all signals for re-evaluation")

            time.sleep(15)

        except KeyboardInterrupt:
            log.info("Shutting down...")
            break
        except Exception as e:
            log.error(f"Session error: {e}", exc_info=True)
            time.sleep(30)


if __name__ == "__main__":
    main()
