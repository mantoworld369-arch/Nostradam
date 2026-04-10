#!/usr/bin/env python3
"""NOSTRADAM v0.4"""
import sys, time, logging, threading
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
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s │ %(name)-20s │ %(levelname)-5s │ %(message)s", datefmt="%H:%M:%S")

def trading_loop(cfg, conn, scanner, analyzer, trader, duration_sec):
    log = logging.getLogger("nostradam.main")
    cycle = cfg["cycle_interval_seconds"]
    start = time.time()

    while time.time() - start < duration_sec:
        try:
            markets = scanner.fetch_btc_minute_markets()
            for market in markets:
                mid = market["id"]
                if not mid: continue
                log_market(conn, market)
                tokens = market.get("token_ids", [])
                if not tokens: continue

                yes_tok = tokens[0] if tokens else None
                no_tok = tokens[1] if len(tokens) > 1 else None
                by = scanner.get_order_book(yes_tok) if yes_tok else None
                bn = scanner.get_order_book(no_tok) if no_tok else None
                py = scanner.parse_book(by) if by else None
                pn = scanner.parse_book(bn) if bn else None
                if not py: continue

                snap = {
                    "best_bid_yes": py["best_bid"], "best_ask_yes": py["best_ask"],
                    "best_bid_no": pn["best_bid"] if pn else 0, "best_ask_no": pn["best_ask"] if pn else 0,
                    "spread_yes": py["spread"], "spread_no": pn["spread"] if pn else 0,
                    "mid_yes": py["mid"], "volume": market.get("volume", 0),
                    "book_depth_yes": py["depth"], "book_depth_no": pn["depth"] if pn else 0,
                }
                log_snapshot(conn, mid, snap)

                snapshots = [dict(s) for s in get_snapshots_for_market(conn, mid)]
                if len(snapshots) < 3: continue

                for signal in analyzer.analyze(snapshots, py, pn):
                    signal.meta["current_mid"] = py["mid"]
                    trader.execute(signal, mid, book_yes=py, book_no=pn)

            trader.update_open_positions(scanner)
            check_resolutions(conn, scanner, trader, log)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)
        time.sleep(cycle)

def check_resolutions(conn, scanner, trader, log):
    now = datetime.now(timezone.utc)
    for trade in get_open_trades(conn):
        mid = trade["market_id"]
        row = conn.execute("SELECT end_time, resolved, outcome FROM markets WHERE id=?", (mid,)).fetchone()
        if not row or not row["end_time"]: continue

        try:
            end_dt = datetime.fromisoformat(row["end_time"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        if now < end_dt: continue

        if row["resolved"]:
            outcome = row["outcome"]
        else:
            # Try immediately, then retry every cycle
            outcome = scanner.fetch_resolution(mid)
            if outcome:
                conn.execute("UPDATE markets SET resolved=1, outcome=? WHERE id=?", (outcome, mid))
                conn.commit()
                log.info(f"Resolved {mid[:12]}... -> {outcome}")
            elif (now - end_dt).total_seconds() > 45:
                # After 45s, try harder with slug-based lookup
                market = scanner.known_markets.get(mid, {})
                slug = market.get("slug", "")
                if slug:
                    m = scanner._fetch_market_by_slug(slug)
                    if m and m.get("resolved") is not False:
                        # Re-fetch from API with updated data
                        outcome = scanner.fetch_resolution(mid)
                if not outcome and (now - end_dt).total_seconds() > 90:
                    log.warning(f"Timeout resolving {mid[:12]}... defaulting NO")
                    outcome = "NO"
                    conn.execute("UPDATE markets SET resolved=1, outcome=? WHERE id=?", (outcome, mid))
                    conn.commit()
                else:
                    continue

        if outcome:
            trader.resolve_market(mid, outcome)

def main():
    cfg = load_config()
    setup_logging(cfg.get("log_level", "INFO"))
    log = logging.getLogger("nostradam.main")

    if not cfg["paper_trade"]:
        log.error("Set paper_trade: true"); sys.exit(1)

    conn = get_db(cfg.get("db_path", "nostradam.db"))
    init_db(conn)

    scanner = MarketScanner(cfg)
    analyzer = Analyzer(cfg)
    trader = PaperTrader(cfg, conn)
    optimizer = Optimizer(cfg, conn)

    if cfg["dashboard"]["enabled"]:
        app = create_app(conn, trader, scanner)
        threading.Thread(target=lambda: app.run(host=cfg["dashboard"]["host"],
            port=cfg["dashboard"]["port"], debug=False, use_reloader=False), daemon=True).start()

    session_min = cfg.get("session_duration_minutes", 30)
    snum = 0

    log.info(f"NOSTRADAM v0.4 | ${cfg['bankroll']} | {session_min}min sessions | cycle {cfg['cycle_interval_seconds']}s")

    while True:
        try:
            snum += 1
            sid = start_session(conn, session_min, {k:v for k,v in cfg.items() if k!="api"})
            trader.set_session(sid)
            log.info(f"\n  SESSION {snum} (id={sid}) START | edge>={cfg['strategy']['min_edge']:.0%} | bet={cfg['max_bet_pct']:.0%}\n")

            trading_loop(cfg, conn, scanner, analyzer, trader, session_min * 60)

            time.sleep(15)
            check_resolutions(conn, scanner, trader, log)

            new_cfg, _ = optimizer.optimize(sid)
            cfg["strategy"] = new_cfg["strategy"]
            cfg["max_bet_pct"] = new_cfg["max_bet_pct"]
            analyzer.update_params(cfg)

            if snum % 5 == 0:
                cfg["strategy"]["enabled_signals"] = [
                    "mean_reversion","book_imbalance","momentum","spread_compression","stale_odds"]
                analyzer.update_params(cfg)

            time.sleep(15)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Session error: {e}", exc_info=True)
            time.sleep(30)

if __name__ == "__main__":
    main()
