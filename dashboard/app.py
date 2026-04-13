"""Dashboard v0.5"""
from flask import Flask, render_template, jsonify, request
from core import database as db

def create_app(conn, trader, scanner):
    app = Flask(__name__, template_folder="templates")

    @app.route("/")
    def index(): return render_template("index.html")

    @app.route("/api/state")
    def api_state():
        perf=db.get_performance(conn); st=trader.get_state()
        w=perf.get("wins",0) or 0; l=perf.get("losses",0) or 0; r=w+l
        sid=trader.session_id; sp=db.get_session_performance(conn,sid) if sid else {}
        sw=sp.get("wins",0) or 0; sl=sp.get("losses",0) or 0; sr=sw+sl
        return jsonify({"trader":st,
            "performance":{"total_trades":perf.get("total",0),"resolved":r,"wins":w,"losses":l,
                "win_rate":round(w/r*100,1) if r>0 else 0,"total_pnl":round(perf.get("total_pnl",0) or 0,2),
                "avg_edge":round((perf.get("avg_edge",0) or 0)*100,2),"open_positions":perf.get("open_positions",0)},
            "live_session":{"id":sid,"total":sp.get("total",0),"wins":sw,"losses":sl,
                "win_rate":round(sw/sr*100,1) if sr>0 else 0,"pnl":round(sp.get("total_pnl",0) or 0,2),"open":sp.get("total",0)-sr},
            "market":scanner.market_state})

    @app.route("/api/trades")
    def api_trades():
        result=[]
        for t in db.get_recent_trades(conn,100):
            d=dict(t)
            if not d.get("resolved") and d.get("current_price") and d.get("entry_price"):
                c,e=d["current_price"],d["entry_price"]
                d["unrealized_pnl"]=round(d["size"]*(c/e)-d["size"],2) if c>0 and e>0 else 0
            else: d["unrealized_pnl"]=None
            result.append(d)
        return jsonify(result)

    @app.route("/api/sessions")
    def api_sessions():
        result=[]
        for s in db.get_sessions(conn,20):
            d=dict(s)
            if d["id"]==trader.session_id and not d.get("ended_at"):
                lv=db.get_session_performance(conn,d["id"])
                d["total_trades"]=lv.get("total",0); d["wins"]=lv.get("wins",0) or 0; d["losses"]=lv.get("losses",0) or 0
                d["total_pnl"]=round(lv.get("total_pnl",0) or 0,2)
                r=d["wins"]+d["losses"]; d["win_rate"]=round(d["wins"]/r*100,1) if r>0 else 0
            result.append(d)
        return jsonify(result)

    @app.route("/api/signals")
    def api_signals(): return jsonify(db.get_signal_performance(conn))

    @app.route("/api/settings", methods=["GET"])
    def get_settings():
        return jsonify({"max_bet_pct":trader.cfg["max_bet_pct"],"min_bet":trader.cfg["min_bet"],
            "max_bet":trader.cfg["max_bet"],"min_edge":trader.cfg["strategy"]["min_edge"],
            "max_daily_loss_pct":trader.cfg["risk"]["max_daily_loss_pct"],
            "max_drawdown_pct":trader.cfg["risk"]["max_drawdown_pct"],
            "max_open_positions":trader.cfg["risk"]["max_open_positions"],
            "cooldown_after_loss":trader.cfg["risk"]["cooldown_after_loss"]})

    @app.route("/api/settings", methods=["POST"])
    def update_settings():
        d=request.json or {}
        for k in ["max_bet_pct","min_bet","max_bet"]:
            if k in d: trader.cfg[k]=float(d[k])
        if "min_edge" in d: trader.cfg["strategy"]["min_edge"]=float(d["min_edge"])
        for k in ["max_daily_loss_pct","max_drawdown_pct"]:
            if k in d: trader.cfg["risk"][k]=float(d[k])
        for k in ["max_open_positions","cooldown_after_loss"]:
            if k in d: trader.cfg["risk"][k]=int(d[k])
        return jsonify({"ok":True})

    return app
