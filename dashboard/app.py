"""
Dashboard: Flask web UI for monitoring Nostradam.
"""

import json
from flask import Flask, render_template, jsonify
from core import database as db


def create_app(conn, trader):
    app = Flask(__name__, template_folder="dashboard/templates")

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/state")
    def api_state():
        perf = db.get_performance(conn)
        state = trader.get_state()
        total = perf.get("total", 0)
        wins = perf.get("wins", 0)
        losses = perf.get("losses", 0)
        resolved = wins + losses
        return jsonify({
            "trader": state,
            "performance": {
                "total_trades": total,
                "resolved": resolved,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / resolved * 100, 1) if resolved > 0 else 0,
                "total_pnl": round(perf.get("total_pnl", 0) or 0, 2),
                "avg_edge": round((perf.get("avg_edge", 0) or 0) * 100, 2),
                "open_positions": perf.get("open_positions", 0),
            }
        })

    @app.route("/api/trades")
    def api_trades():
        trades = db.get_recent_trades(conn, limit=100)
        return jsonify([dict(t) for t in trades])

    @app.route("/api/snapshots")
    def api_snapshots():
        snaps = db.get_recent_snapshots(conn, limit=300)
        return jsonify([dict(s) for s in snaps])

    return app
