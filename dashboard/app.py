"""Dashboard with live settings control and live session stats."""

from flask import Flask, render_template, jsonify, request
from core import database as db


def create_app(conn, trader, scanner):
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

        # Live session stats
        sid = trader.session_id
        sess_perf = db.get_session_performance(conn, sid) if sid else {}
        s_total = sess_perf.get("total", 0)
        s_wins = sess_perf.get("wins", 0) or 0
        s_losses = sess_perf.get("losses", 0) or 0
        s_resolved = s_wins + s_losses

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
            },
            "live_session": {
                "id": sid,
                "total": s_total,
                "wins": s_wins,
                "losses": s_losses,
                "win_rate": round(s_wins / s_resolved * 100, 1) if s_resolved > 0 else 0,
                "pnl": round(sess_perf.get("total_pnl", 0) or 0, 2),
                "open": s_total - s_resolved,
            },
            "market": scanner.active_market_state,
        })

    @app.route("/api/trades")
    def api_trades():
        trades = db.get_recent_trades(conn, limit=100)
        result = []
        for t in trades:
            d = dict(t)
            if not d.get("resolved") and d.get("current_price") and d.get("entry_price"):
                cur = d["current_price"]
                entry = d["entry_price"]
                if cur > 0 and entry > 0:
                    d["unrealized_pnl"] = round(d["size"] * (cur / entry) - d["size"], 2)
                else:
                    d["unrealized_pnl"] = 0
            else:
                d["unrealized_pnl"] = None
            result.append(d)
        return jsonify(result)

    @app.route("/api/sessions")
    def api_sessions():
        sessions = db.get_sessions(conn, limit=20)
        result = []
        for s in sessions:
            d = dict(s)
            # If this is the active session, inject live stats
            if d["id"] == trader.session_id and not d.get("ended_at"):
                live = db.get_session_performance(conn, d["id"])
                d["total_trades"] = live.get("total", 0)
                d["wins"] = live.get("wins", 0) or 0
                d["losses"] = live.get("losses", 0) or 0
                d["total_pnl"] = round(live.get("total_pnl", 0) or 0, 2)
                resolved = d["wins"] + d["losses"]
                d["win_rate"] = round(d["wins"] / resolved * 100, 1) if resolved > 0 else 0
            result.append(d)
        return jsonify(result)

    @app.route("/api/signals")
    def api_signals():
        return jsonify(db.get_signal_performance(conn))

    @app.route("/api/settings", methods=["GET"])
    def get_settings():
        return jsonify({
            "max_bet_pct": trader.cfg["max_bet_pct"],
            "min_bet": trader.cfg["min_bet"],
            "max_bet": trader.cfg["max_bet"],
            "min_edge": trader.cfg["strategy"]["min_edge"],
            "max_daily_loss_pct": trader.cfg["risk"]["max_daily_loss_pct"],
            "max_drawdown_pct": trader.cfg["risk"]["max_drawdown_pct"],
            "max_open_positions": trader.cfg["risk"]["max_open_positions"],
            "cooldown_after_loss": trader.cfg["risk"]["cooldown_after_loss"],
        })

    @app.route("/api/settings", methods=["POST"])
    def update_settings():
        data = request.json
        if not data:
            return jsonify({"error": "no data"}), 400

        # Update trader config live
        if "max_bet_pct" in data:
            trader.cfg["max_bet_pct"] = float(data["max_bet_pct"])
        if "min_bet" in data:
            trader.cfg["min_bet"] = float(data["min_bet"])
        if "max_bet" in data:
            trader.cfg["max_bet"] = float(data["max_bet"])
        if "min_edge" in data:
            trader.cfg["strategy"]["min_edge"] = float(data["min_edge"])
        if "max_daily_loss_pct" in data:
            trader.cfg["risk"]["max_daily_loss_pct"] = float(data["max_daily_loss_pct"])
        if "max_drawdown_pct" in data:
            trader.cfg["risk"]["max_drawdown_pct"] = float(data["max_drawdown_pct"])
        if "max_open_positions" in data:
            trader.cfg["risk"]["max_open_positions"] = int(data["max_open_positions"])
        if "cooldown_after_loss" in data:
            trader.cfg["risk"]["cooldown_after_loss"] = int(data["cooldown_after_loss"])

        return jsonify({"ok": True})

    return app
