"""Dashboard: Flask web UI for Nostradam."""

from flask import Flask, render_template, jsonify
from core import database as db


def create_app(conn, trader):
    app = Flask(__name__, template_folder="templates")

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/state")
    def api_state():
        perf = db.get_performance(conn)
        state = trader.get_state()
        total = perf.get("total", 0)
        wins = perf.get("wins", 0) or 0
        losses = perf.get("losses", 0) or 0
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
        result = []
        for t in trades:
            d = dict(t)
            # Calculate unrealized P&L for open positions
            if not d.get("resolved") and d.get("current_price") and d.get("entry_price"):
                cur = d["current_price"]
                entry = d["entry_price"]
                size = d["size"]
                # If current bid > entry price, we're in profit
                if cur > 0:
                    current_value = size * (cur / entry)
                    d["unrealized_pnl"] = round(current_value - size, 2)
                else:
                    d["unrealized_pnl"] = 0
            else:
                d["unrealized_pnl"] = None
            result.append(d)
        return jsonify(result)

    @app.route("/api/sessions")
    def api_sessions():
        sessions = db.get_sessions(conn, limit=20)
        return jsonify([dict(s) for s in sessions])

    @app.route("/api/signals")
    def api_signals():
        return jsonify(db.get_signal_performance(conn))

    return app
