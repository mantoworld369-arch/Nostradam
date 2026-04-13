#!/usr/bin/env python3
"""NOSTRADAM v0.5"""
import sys,time,logging,threading
from datetime import datetime,timezone
from core.config import load_config
from core.database import *
from core.scanner import MarketScanner
from core.analyzer import Analyzer
from core.trader import PaperTrader
from core.optimizer import Optimizer
from dashboard.app import create_app

def setup_logging(level):
    logging.basicConfig(level=getattr(logging,level.upper(),logging.INFO),
        format="%(asctime)s | %(name)-20s | %(levelname)-5s | %(message)s",datefmt="%H:%M:%S")

def trading_loop(cfg,conn,scanner,analyzer,trader,dur):
    log=logging.getLogger("nostradam.main"); cycle=cfg["cycle_interval_seconds"]; start=time.time()
    while time.time()-start<dur:
        try:
            markets=scanner.fetch_btc_minute_markets()
            for m in markets:
                mid=m["id"]
                if not mid: continue
                log_market(conn,m); tids=m.get("token_ids",[])
                if not tids: continue
                by=scanner.get_order_book(tids[0]) if tids else None
                bn=scanner.get_order_book(tids[1]) if len(tids)>1 else None
                py=scanner.parse_book(by); pn=scanner.parse_book(bn)
                if not py: continue
                snap={"best_bid_yes":py["best_bid"],"best_ask_yes":py["best_ask"],
                      "best_bid_no":pn["best_bid"] if pn else 0,"best_ask_no":pn["best_ask"] if pn else 0,
                      "spread_yes":py["spread"],"spread_no":pn["spread"] if pn else 0,
                      "mid_yes":py["mid"],"volume":m.get("volume",0),
                      "book_depth_yes":py["depth"],"book_depth_no":pn["depth"] if pn else 0}
                log_snapshot(conn,mid,snap)
                snaps=[dict(s) for s in get_snapshots_for_market(conn,mid)]
                if len(snaps)<3: continue
                for sig in analyzer.analyze(snaps,py,pn):
                    sig.meta["current_mid"]=py["mid"]
                    trader.execute(sig,mid,book_yes=py,book_no=pn)
            trader.update_open_positions(scanner)
            _resolve(conn,scanner,trader,log)
        except KeyboardInterrupt: raise
        except Exception as e: log.error(f"Loop err: {e}",exc_info=True)
        time.sleep(cycle)

def _resolve(conn,scanner,trader,log):
    now=datetime.now(timezone.utc)
    for t in get_open_trades(conn):
        mid=t["market_id"]
        row=conn.execute("SELECT end_time,resolved,outcome FROM markets WHERE id=?",(mid,)).fetchone()
        if not row or not row["end_time"]: continue
        try: end=datetime.fromisoformat(row["end_time"].replace("Z","+00:00"))
        except: continue
        if now<end: continue
        if row["resolved"]: outcome=row["outcome"]
        else:
            outcome=scanner.fetch_resolution(mid)
            if outcome:
                conn.execute("UPDATE markets SET resolved=1,outcome=? WHERE id=?",(outcome,mid)); conn.commit()
            elif (now-end).total_seconds()>90:
                outcome="NO"; conn.execute("UPDATE markets SET resolved=1,outcome=? WHERE id=?",(outcome,mid)); conn.commit()
            else: continue
        if outcome: trader.resolve_market(mid,outcome)

def main():
    cfg=load_config(); setup_logging(cfg.get("log_level","INFO"))
    log=logging.getLogger("nostradam.main")
    if not cfg["paper_trade"]: log.error("Set paper_trade: true"); sys.exit(1)
    conn=get_db(cfg.get("db_path","nostradam.db")); init_db(conn)
    scanner=MarketScanner(cfg); analyzer=Analyzer(cfg)
    trader=PaperTrader(cfg,conn); optimizer=Optimizer(cfg,conn)
    if cfg["dashboard"]["enabled"]:
        app=create_app(conn,trader,scanner)
        threading.Thread(target=lambda:app.run(host=cfg["dashboard"]["host"],port=cfg["dashboard"]["port"],debug=False,use_reloader=False),daemon=True).start()
    smin=cfg.get("session_duration_minutes",30); snum=0
    log.info(f"NOSTRADAM v0.5 | ${cfg['bankroll']} | {smin}min sessions")
    while True:
        try:
            snum+=1; sid=start_session(conn,smin,{k:v for k,v in cfg.items() if k!="api"})
            trader.set_session(sid)
            log.info(f"\n  SESSION {snum} (id={sid}) | edge>={cfg['strategy']['min_edge']:.0%} | bet={cfg['max_bet_pct']:.0%}\n")
            trading_loop(cfg,conn,scanner,analyzer,trader,smin*60)
            time.sleep(15); _resolve(conn,scanner,trader,log)
            new,_=optimizer.optimize(sid)
            cfg["strategy"]=new["strategy"]; cfg["max_bet_pct"]=new["max_bet_pct"]
            analyzer.update_params(cfg)
            if snum%5==0:
                cfg["strategy"]["enabled_signals"]=["trend_follow","contrarian","book_imbalance","momentum","spread_compression","volatility_spike","odds_divergence"]
                analyzer.update_params(cfg)
            time.sleep(15)
        except KeyboardInterrupt: break
        except Exception as e: log.error(f"Session err: {e}",exc_info=True); time.sleep(30)

if __name__=="__main__": main()
