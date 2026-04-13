"""Optimizer v0.5"""
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
        with open(f"session_history/s_{sid:04d}.json","w") as f:
            json.dump({"sid":sid,"perf":{"w":w,"l":l,"pnl":pnl},"notes":notes},f,indent=2,default=str)
        db.end_session(self.conn,sid,"\n".join(notes))
        return new,"\n".join(notes)
