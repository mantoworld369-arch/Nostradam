"""Optimizer v0.5.1: now includes side bias learning."""
import logging, json, copy, os
from datetime import datetime, timezone
from core import database as db
log = logging.getLogger("nostradam.optimizer")

class Optimizer:
    def __init__(self, cfg, conn):
        self.cfg=cfg; self.conn=conn; os.makedirs("session_history",exist_ok=True)

    def optimize(self, sid):
        log.info(f"OPTIMIZING Session {sid}")
        notes=[]; new=copy.deepcopy(self.cfg)
        p=db.get_session_performance(self.conn,sid)
        sp=db.get_signal_performance(self.conn,sid)
        ep=db.get_edge_range_performance(self.conn,sid)
        w=p.get("wins",0) or 0; l=p.get("losses",0) or 0; r=w+l; pnl=p.get("total_pnl",0) or 0
        if r<3:
            notes.append("Too few trades"); db.end_session(self.conn,sid,"\n".join(notes))
            return new,"\n".join(notes)
        wr=w/r
        enabled=list(new["strategy"].get("enabled_signals",[]))
        for s in sp:
            st,stot,sw,spnl=s["signal_type"],s["total"],s["wins"] or 0,s["pnl"] or 0
            swr=sw/stot if stot>0 else 0
            notes.append(f"{st}: {sw}/{stot} ({swr:.0%}) ${spnl:.2f}")
            if stot>=5 and swr<0.30 and spnl<0 and st in enabled and len(enabled)>1:
                enabled.remove(st); notes.append(f"  DISABLED {st}")
        new["strategy"]["enabled_signals"]=enabled
        low=[e for e in ep if e["bucket"]=="low"]
        if low and low[0]["total"]>=3 and (low[0]["pnl"] or 0)<0:
            new["strategy"]["min_edge"]=min(new["strategy"]["min_edge"]+0.01,0.15)
        if wr>0.55 and pnl>0: new["max_bet_pct"]=min(new["max_bet_pct"]*1.1,0.10)
        elif wr<0.35 and pnl<0: new["max_bet_pct"]=max(new["max_bet_pct"]*0.8,0.02)

        # === SIDE BIAS LEARNING ===
        # Look at last 50 resolved trades across all sessions for side performance
        side_perf=db.get_side_performance(self.conn, last_n=50)
        yes_d=side_perf.get("YES",{}); no_d=side_perf.get("NO",{})
        yes_total=yes_d.get("total",0); no_total=no_d.get("total",0)
        yes_wins=yes_d.get("wins",0) or 0; no_wins=no_d.get("wins",0) or 0
        yes_wr=yes_wins/yes_total if yes_total>=5 else 0.5
        no_wr=no_wins/no_total if no_total>=5 else 0.5
        notes.append(f"SIDE: YES {yes_wins}/{yes_total} ({yes_wr:.0%}) | NO {no_wins}/{no_total} ({no_wr:.0%})")

        if yes_total>=5 and no_total>=5:
            # Ratio of win rates: >1 means YES wins more, <1 means NO wins more
            # Clamp between 0.3 and 3.0 to prevent extreme swings
            raw_bias = yes_wr / no_wr if no_wr > 0 else 2.0
            # Smooth: blend 60% new data, 40% old bias to prevent whiplash
            old_bias = new["strategy"].get("side_bias", 1.0)
            new_bias = round(max(0.3, min(3.0, raw_bias * 0.6 + old_bias * 0.4)), 3)
            new["strategy"]["side_bias"] = new_bias
            notes.append(f"  SIDE BIAS: {old_bias:.2f} -> {new_bias:.2f} (raw={raw_bias:.2f})")
        elif yes_total>=5 and yes_wr<0.35:
            # Only YES data, and it's bad — shift toward NO
            new["strategy"]["side_bias"] = max(0.3, new["strategy"].get("side_bias", 1.0) * 0.7)
            notes.append(f"  YES struggling ({yes_wr:.0%}), bias -> {new['strategy']['side_bias']:.2f}")
        elif no_total>=5 and no_wr<0.35:
            # Only NO data, and it's bad — shift toward YES
            new["strategy"]["side_bias"] = min(3.0, new["strategy"].get("side_bias", 1.0) * 1.4)
            notes.append(f"  NO struggling ({no_wr:.0%}), bias -> {new['strategy']['side_bias']:.2f}")

        with open(f"session_history/s_{sid:04d}.json","w") as f:
            json.dump({"sid":sid,"perf":{"w":w,"l":l,"pnl":pnl},"side":{"yes_wr":yes_wr,"no_wr":no_wr},"notes":notes},f,indent=2,default=str)
        db.end_session(self.conn,sid,"\n".join(notes))
        return new,"\n".join(notes)
