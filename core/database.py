import sqlite3, json
from datetime import datetime, timezone

def get_db(path="nostradam.db"):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS markets (id TEXT PRIMARY KEY, condition_id TEXT, question TEXT, slug TEXT, discovered_at TEXT, end_time TEXT, resolved INTEGER DEFAULT 0, outcome TEXT);
    CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT, ts TEXT, best_bid_yes REAL, best_ask_yes REAL, best_bid_no REAL, best_ask_no REAL, spread_yes REAL, spread_no REAL, mid_yes REAL, volume REAL, book_depth_yes REAL, book_depth_no REAL);
    CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT, session_id INTEGER DEFAULT 0, ts_entry TEXT, ts_exit TEXT, side TEXT, entry_price REAL, entry_mid REAL, entry_ask REAL, entry_spread REAL, current_price REAL, exit_price REAL, size REAL, pnl REAL, edge_at_entry REAL, signal_type TEXT, resolved INTEGER DEFAULT 0, won INTEGER, meta TEXT);
    CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, ended_at TEXT, duration_minutes INTEGER, total_trades INTEGER DEFAULT 0, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0, total_pnl REAL DEFAULT 0, win_rate REAL DEFAULT 0, avg_edge REAL DEFAULT 0, best_signal TEXT, worst_signal TEXT, config_snapshot TEXT, optimization_notes TEXT);
    CREATE INDEX IF NOT EXISTS idx_snap_mkt ON snapshots(market_id, ts);
    CREATE INDEX IF NOT EXISTS idx_trade_mkt ON trades(market_id);
    CREATE INDEX IF NOT EXISTS idx_trade_sess ON trades(session_id);
    """)
    conn.commit()

def log_market(conn, m):
    conn.execute("INSERT OR IGNORE INTO markets (id,condition_id,question,slug,discovered_at,end_time) VALUES (?,?,?,?,?,?)",
        (m["id"],m.get("condition_id",""),m.get("question",""),m.get("slug",""),datetime.now(timezone.utc).isoformat(),m.get("end_time","")))
    conn.commit()

def log_snapshot(conn, mid, s):
    conn.execute("INSERT INTO snapshots (market_id,ts,best_bid_yes,best_ask_yes,best_bid_no,best_ask_no,spread_yes,spread_no,mid_yes,volume,book_depth_yes,book_depth_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid,datetime.now(timezone.utc).isoformat(),s["best_bid_yes"],s["best_ask_yes"],s["best_bid_no"],s["best_ask_no"],s["spread_yes"],s["spread_no"],s["mid_yes"],s.get("volume",0),s.get("book_depth_yes",0),s.get("book_depth_no",0)))
    conn.commit()

def log_trade(conn, t):
    conn.execute("INSERT INTO trades (market_id,session_id,ts_entry,side,entry_price,entry_mid,entry_ask,entry_spread,size,edge_at_entry,signal_type,meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (t["market_id"],t.get("session_id",0),datetime.now(timezone.utc).isoformat(),t["side"],t["entry_price"],t.get("entry_mid",0),t.get("entry_ask",0),t.get("entry_spread",0),t["size"],t["edge"],t["signal_type"],json.dumps(t.get("meta",{}))))
    conn.commit()

def update_trade_price(conn, tid, price):
    conn.execute("UPDATE trades SET current_price=? WHERE id=?", (price, tid)); conn.commit()

def resolve_trade(conn, tid, exit_price, pnl, won):
    conn.execute("UPDATE trades SET ts_exit=?,exit_price=?,pnl=?,resolved=1,won=?,current_price=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(),exit_price,pnl,int(won),exit_price,tid)); conn.commit()

def get_recent_trades(conn, limit=50): return conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
def get_open_trades(conn): return conn.execute("SELECT * FROM trades WHERE resolved=0").fetchall()

def get_performance(conn):
    r = conn.execute("SELECT COUNT(*) as total, SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins, SUM(CASE WHEN won=0 AND resolved=1 THEN 1 ELSE 0 END) as losses, SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END) as total_pnl, AVG(CASE WHEN resolved=1 THEN edge_at_entry END) as avg_edge, COUNT(CASE WHEN resolved=0 THEN 1 END) as open_positions FROM trades").fetchone()
    return dict(r) if r else {}

def get_session_performance(conn, sid):
    r = conn.execute("SELECT COUNT(*) as total, SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins, SUM(CASE WHEN won=0 AND resolved=1 THEN 1 ELSE 0 END) as losses, SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END) as total_pnl, AVG(CASE WHEN resolved=1 THEN edge_at_entry END) as avg_edge FROM trades WHERE session_id=?", (sid,)).fetchone()
    return dict(r) if r else {}

def get_signal_performance(conn, sid=None):
    q = "SELECT signal_type, COUNT(*) as total, SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins, SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END) as pnl, AVG(edge_at_entry) as avg_edge FROM trades WHERE resolved=1"
    if sid: q += f" AND session_id={sid}"
    return [dict(r) for r in conn.execute(q + " GROUP BY signal_type").fetchall()]

def get_edge_range_performance(conn, sid=None):
    w = f"AND session_id={sid}" if sid else ""
    return [dict(r) for r in conn.execute(f"SELECT CASE WHEN edge_at_entry<0.05 THEN 'low' WHEN edge_at_entry<0.10 THEN 'mid' WHEN edge_at_entry<0.20 THEN 'high' ELSE 'extreme' END as bucket, COUNT(*) as total, SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins, SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END) as pnl FROM trades WHERE resolved=1 {w} GROUP BY bucket").fetchall()]

def start_session(conn, dur, cfg_snap):
    c = conn.execute("INSERT INTO sessions (started_at,duration_minutes,config_snapshot) VALUES (?,?,?)", (datetime.now(timezone.utc).isoformat(),dur,json.dumps(cfg_snap)))
    conn.commit(); return c.lastrowid

def end_session(conn, sid, notes=""):
    p = get_session_performance(conn, sid); w=p.get("wins",0) or 0; l=p.get("losses",0) or 0; r=w+l
    sp = get_signal_performance(conn, sid)
    best = max(sp, key=lambda x: x.get("pnl",0))["signal_type"] if sp else ""
    worst = min(sp, key=lambda x: x.get("pnl",0))["signal_type"] if sp else ""
    conn.execute("UPDATE sessions SET ended_at=?,total_trades=?,wins=?,losses=?,total_pnl=?,win_rate=?,avg_edge=?,best_signal=?,worst_signal=?,optimization_notes=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(),p.get("total",0),w,l,p.get("total_pnl",0) or 0,round(w/r*100,1) if r>0 else 0,p.get("avg_edge",0) or 0,best,worst,notes,sid))
    conn.commit()

def get_sessions(conn, limit=20): return conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
def get_snapshots_for_market(conn, mid): return conn.execute("SELECT * FROM snapshots WHERE market_id=? ORDER BY ts", (mid,)).fetchall()
