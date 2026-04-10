import sqlite3
import json
from datetime import datetime, timezone


def get_db(path="nostradam.db"):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS markets (
        id TEXT PRIMARY KEY,
        condition_id TEXT,
        question TEXT,
        slug TEXT,
        discovered_at TEXT,
        end_time TEXT,
        resolved INTEGER DEFAULT 0,
        outcome TEXT
    );

    CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id TEXT,
        ts TEXT,
        best_bid_yes REAL,
        best_ask_yes REAL,
        best_bid_no REAL,
        best_ask_no REAL,
        spread_yes REAL,
        spread_no REAL,
        mid_yes REAL,
        volume REAL,
        book_depth_yes REAL,
        book_depth_no REAL
    );

    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id TEXT,
        session_id INTEGER DEFAULT 0,
        ts_entry TEXT,
        ts_exit TEXT,
        side TEXT,
        entry_price REAL,
        entry_mid REAL,
        entry_ask REAL,
        entry_spread REAL,
        current_price REAL,
        exit_price REAL,
        size REAL,
        pnl REAL,
        edge_at_entry REAL,
        signal_type TEXT,
        resolved INTEGER DEFAULT 0,
        won INTEGER,
        meta TEXT
    );

    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT,
        ended_at TEXT,
        duration_minutes INTEGER,
        total_trades INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0,
        total_pnl REAL DEFAULT 0,
        win_rate REAL DEFAULT 0,
        avg_edge REAL DEFAULT 0,
        best_signal TEXT,
        worst_signal TEXT,
        config_snapshot TEXT,
        optimization_notes TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_snapshots_market ON snapshots(market_id, ts);
    CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
    CREATE INDEX IF NOT EXISTS idx_trades_session ON trades(session_id);
    """)
    conn.commit()


def log_market(conn, market):
    conn.execute(
        "INSERT OR IGNORE INTO markets (id, condition_id, question, slug, discovered_at, end_time) VALUES (?,?,?,?,?,?)",
        (market["id"], market.get("condition_id", ""), market.get("question", ""),
         market.get("slug", ""), datetime.now(timezone.utc).isoformat(), market.get("end_time", ""))
    )
    conn.commit()


def log_snapshot(conn, market_id, snap):
    conn.execute(
        "INSERT INTO snapshots (market_id, ts, best_bid_yes, best_ask_yes, best_bid_no, best_ask_no, spread_yes, spread_no, mid_yes, volume, book_depth_yes, book_depth_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (market_id, datetime.now(timezone.utc).isoformat(),
         snap["best_bid_yes"], snap["best_ask_yes"],
         snap["best_bid_no"], snap["best_ask_no"],
         snap["spread_yes"], snap["spread_no"],
         snap["mid_yes"], snap.get("volume", 0),
         snap.get("book_depth_yes", 0), snap.get("book_depth_no", 0))
    )
    conn.commit()


def log_trade(conn, trade):
    conn.execute(
        """INSERT INTO trades (market_id, session_id, ts_entry, side, entry_price, entry_mid, entry_ask, entry_spread, size, edge_at_entry, signal_type, meta)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trade["market_id"], trade.get("session_id", 0),
         datetime.now(timezone.utc).isoformat(),
         trade["side"], trade["entry_price"],
         trade.get("entry_mid", 0), trade.get("entry_ask", 0), trade.get("entry_spread", 0),
         trade["size"], trade["edge"], trade["signal_type"],
         json.dumps(trade.get("meta", {})))
    )
    conn.commit()


def update_trade_current_price(conn, trade_id, current_price):
    conn.execute("UPDATE trades SET current_price=? WHERE id=?", (current_price, trade_id))
    conn.commit()


def resolve_trade(conn, trade_id, exit_price, pnl, won):
    conn.execute(
        "UPDATE trades SET ts_exit=?, exit_price=?, pnl=?, resolved=1, won=?, current_price=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), exit_price, pnl, int(won), exit_price, trade_id)
    )
    conn.commit()


def get_recent_trades(conn, limit=50):
    return conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get_open_trades(conn):
    return conn.execute("SELECT * FROM trades WHERE resolved=0").fetchall()


def get_session_trades(conn, session_id):
    return conn.execute("SELECT * FROM trades WHERE session_id=?", (session_id,)).fetchall()


def get_performance(conn):
    row = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN won=0 AND resolved=1 THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END) as total_pnl,
            AVG(CASE WHEN resolved=1 THEN edge_at_entry END) as avg_edge,
            COUNT(CASE WHEN resolved=0 THEN 1 END) as open_positions
        FROM trades
    """).fetchone()
    return dict(row) if row else {}


def get_session_performance(conn, session_id):
    row = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN won=0 AND resolved=1 THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END) as total_pnl,
            AVG(CASE WHEN resolved=1 THEN edge_at_entry END) as avg_edge
        FROM trades WHERE session_id=?
    """, (session_id,)).fetchone()
    return dict(row) if row else {}


def get_signal_performance(conn, session_id=None):
    if session_id:
        rows = conn.execute("""
            SELECT signal_type, COUNT(*) as total,
                SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END) as pnl,
                AVG(edge_at_entry) as avg_edge
            FROM trades WHERE resolved=1 AND session_id=? GROUP BY signal_type
        """, (session_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT signal_type, COUNT(*) as total,
                SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END) as pnl,
                AVG(edge_at_entry) as avg_edge
            FROM trades WHERE resolved=1 GROUP BY signal_type
        """).fetchall()
    return [dict(r) for r in rows]


def get_edge_range_performance(conn, session_id=None):
    where = f"AND session_id={session_id}" if session_id else ""
    rows = conn.execute(f"""
        SELECT
            CASE
                WHEN edge_at_entry < 0.05 THEN 'low_3-5'
                WHEN edge_at_entry < 0.10 THEN 'mid_5-10'
                WHEN edge_at_entry < 0.20 THEN 'high_10-20'
                ELSE 'extreme_20+'
            END as edge_bucket,
            COUNT(*) as total,
            SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END) as pnl
        FROM trades WHERE resolved=1 {where} GROUP BY edge_bucket
    """).fetchall()
    return [dict(r) for r in rows]


def get_side_performance(conn, session_id=None):
    where = f"AND session_id={session_id}" if session_id else ""
    rows = conn.execute(f"""
        SELECT side, COUNT(*) as total,
            SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN resolved=1 THEN pnl ELSE 0 END) as pnl
        FROM trades WHERE resolved=1 {where} GROUP BY side
    """).fetchall()
    return [dict(r) for r in rows]


def start_session(conn, duration_minutes, config_snapshot):
    cur = conn.execute(
        "INSERT INTO sessions (started_at, duration_minutes, config_snapshot) VALUES (?,?,?)",
        (datetime.now(timezone.utc).isoformat(), duration_minutes, json.dumps(config_snapshot))
    )
    conn.commit()
    return cur.lastrowid


def end_session(conn, session_id, optimization_notes=""):
    perf = get_session_performance(conn, session_id)
    total = perf.get("total", 0)
    wins = perf.get("wins", 0) or 0
    losses = perf.get("losses", 0) or 0
    resolved = wins + losses

    signal_perf = get_signal_performance(conn, session_id)
    best = max(signal_perf, key=lambda x: x.get("pnl", 0))["signal_type"] if signal_perf else ""
    worst = min(signal_perf, key=lambda x: x.get("pnl", 0))["signal_type"] if signal_perf else ""

    conn.execute(
        """UPDATE sessions SET ended_at=?, total_trades=?, wins=?, losses=?, total_pnl=?,
           win_rate=?, avg_edge=?, best_signal=?, worst_signal=?, optimization_notes=?
           WHERE id=?""",
        (datetime.now(timezone.utc).isoformat(), total, wins, losses,
         perf.get("total_pnl", 0) or 0,
         round(wins / resolved * 100, 1) if resolved > 0 else 0,
         perf.get("avg_edge", 0) or 0,
         best, worst, optimization_notes, session_id)
    )
    conn.commit()


def get_sessions(conn, limit=20):
    return conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get_snapshots_for_market(conn, market_id):
    return conn.execute("SELECT * FROM snapshots WHERE market_id=? ORDER BY ts", (market_id,)).fetchall()


def get_recent_snapshots(conn, limit=200):
    return conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
