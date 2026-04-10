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
        book_depth_no REAL,
        FOREIGN KEY(market_id) REFERENCES markets(id)
    );

    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id TEXT,
        ts_entry TEXT,
        ts_exit TEXT,
        side TEXT,           -- 'YES' or 'NO'
        entry_price REAL,
        exit_price REAL,
        size REAL,
        pnl REAL,
        edge_at_entry REAL,
        signal_type TEXT,
        resolved INTEGER DEFAULT 0,
        won INTEGER,
        meta TEXT,
        FOREIGN KEY(market_id) REFERENCES markets(id)
    );

    CREATE TABLE IF NOT EXISTS stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        bankroll REAL,
        total_trades INTEGER,
        wins INTEGER,
        losses INTEGER,
        win_rate REAL,
        total_pnl REAL,
        avg_edge REAL,
        sharpe REAL,
        max_drawdown REAL
    );

    CREATE INDEX IF NOT EXISTS idx_snapshots_market ON snapshots(market_id, ts);
    CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
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
        "INSERT INTO trades (market_id, ts_entry, side, entry_price, size, edge_at_entry, signal_type, meta) VALUES (?,?,?,?,?,?,?,?)",
        (trade["market_id"], datetime.now(timezone.utc).isoformat(),
         trade["side"], trade["entry_price"], trade["size"],
         trade["edge"], trade["signal_type"], json.dumps(trade.get("meta", {})))
    )
    conn.commit()


def resolve_trade(conn, trade_id, exit_price, pnl, won):
    conn.execute(
        "UPDATE trades SET ts_exit=?, exit_price=?, pnl=?, resolved=1, won=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), exit_price, pnl, int(won), trade_id)
    )
    conn.commit()


def get_recent_trades(conn, limit=50):
    return conn.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def get_open_trades(conn):
    return conn.execute(
        "SELECT * FROM trades WHERE resolved=0"
    ).fetchall()


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


def get_snapshots_for_market(conn, market_id):
    return conn.execute(
        "SELECT * FROM snapshots WHERE market_id=? ORDER BY ts", (market_id,)
    ).fetchall()


def get_recent_snapshots(conn, limit=200):
    return conn.execute(
        "SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
