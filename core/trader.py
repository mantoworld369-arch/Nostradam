"""Trader v0.5: Auto-resets drawdown each session."""
import logging
from core import database as db
log = logging.getLogger("nostradam.trader")

class PaperTrader:
    def __init__(self, cfg, conn):
        self.cfg=cfg; self.conn=conn; self.bankroll=cfg["bankroll"]
        self.peak_bankroll=self.bankroll; self.daily_pnl=0.0
        self.consecutive_losses=0; self.cooldown_remaining=0; self.session_id=0
        self.last_known_prices={}

    def set_session(self, sid):
        self.session_id=sid
        # Reset daily PnL each session so bot doesn't stay stuck
        self.daily_pnl=0.0

    def execute(self, signal, mid, book_yes=None, book_no=None):
        if not self._risk_check(): return None
        if self.cooldown_remaining>0: self.cooldown_remaining-=1; return None
        if len(db.get_open_trades(self.conn))>=self.cfg["risk"]["max_open_positions"]: return None

        mp=signal.meta.get("current_mid",0.5)
        if signal.side=="YES" and book_yes:
            ea=book_yes.get("best_ask",mp); es=book_yes.get("spread",0)
        elif signal.side=="NO" and book_no:
            ea=book_no.get("best_ask",1-mp); es=book_no.get("spread",0)
        else:
            ea=mp+0.02 if signal.side=="YES" else (1-mp)+0.02; es=0.04
        em=mp if signal.side=="YES" else (1-mp)

        if ea<0.02 or ea>0.98: return None
        re_edge=(mp+signal.edge)-ea if signal.side=="YES" else ((1-mp)+signal.edge)-ea
        if re_edge<self.cfg["strategy"]["min_edge"]*0.5: return None

        size=self._size(signal,re_edge)
        if size<self.cfg["min_bet"]: return None

        t={"market_id":mid,"session_id":self.session_id,"side":signal.side,"entry_price":ea,
           "entry_mid":em,"entry_ask":ea,"entry_spread":es,"size":size,"edge":re_edge,
           "signal_type":signal.signal_type,"meta":signal.meta}
        db.log_trade(self.conn,t); self.bankroll-=size
        log.info(f"TRADE: {signal.side} {mid[:12]}... ${size:.2f} @{ea:.3f} edge={re_edge:.3f} [{signal.signal_type}]")
        return t

    def update_open_positions(self, scanner):
        for t in db.get_open_trades(self.conn):
            m=scanner.known_markets.get(t["market_id"])
            if not m: continue
            tids=m.get("token_ids",[])
            if t["side"]=="YES" and tids: bk=scanner.get_order_book(tids[0])
            elif t["side"]=="NO" and len(tids)>=2: bk=scanner.get_order_book(tids[1])
            else: continue
            p=scanner.parse_book(bk)
            if p and p["best_bid"]>0:
                self.last_known_prices[t["id"]]=p["best_bid"]
                db.update_trade_price(self.conn,t["id"],p["best_bid"])
            elif t["id"] in self.last_known_prices:
                db.update_trade_price(self.conn,t["id"],self.last_known_prices[t["id"]])

    def resolve_market(self, market_id, outcome):
        for t in db.get_open_trades(self.conn):
            if t["market_id"]!=market_id: continue
            won=(t["side"]==outcome)
            if won:
                payout=t["size"]/t["entry_price"] if t["entry_price"]>0 else 0
                pnl=payout-t["size"]; self.bankroll+=payout; self.consecutive_losses=0
            else:
                pnl=-t["size"]; self.consecutive_losses+=1
            self.daily_pnl+=pnl
            if self.consecutive_losses>=3: self.cooldown_remaining=self.cfg["risk"]["cooldown_after_loss"]
            if self.bankroll>self.peak_bankroll: self.peak_bankroll=self.bankroll
            db.resolve_trade(self.conn,t["id"],1.0 if won else 0.0,pnl,won)
            self.last_known_prices.pop(t["id"],None)
            log.info(f"{'WIN' if won else 'LOSS'} | {t['side']} @{t['entry_price']:.3f} | ${pnl:+.2f} | bank=${self.bankroll:.2f}")

    def _risk_check(self):
        if self.daily_pnl<-(self.cfg["bankroll"]*self.cfg["risk"]["max_daily_loss_pct"]): return False
        dd=(self.peak_bankroll-self.bankroll)/self.peak_bankroll if self.peak_bankroll>0 else 0
        return dd<=self.cfg["risk"]["max_drawdown_pct"]

    def _size(self, sig, edge):
        base=self.bankroll*self.cfg["max_bet_pct"]
        mult=min(edge/max(self.cfg["strategy"]["min_edge"],0.01),2.0)
        s=base*mult*sig.confidence
        return round(min(max(s,self.cfg["min_bet"]),self.cfg["max_bet"],self.bankroll*0.1),2)

    def reset_daily(self): self.daily_pnl=0.0

    def get_state(self):
        return {"bankroll":round(self.bankroll,2),"peak":round(self.peak_bankroll,2),
                "daily_pnl":round(self.daily_pnl,2),"session_id":self.session_id,
                "drawdown":round((self.peak_bankroll-self.bankroll)/self.peak_bankroll*100,2) if self.peak_bankroll>0 else 0,
                "consecutive_losses":self.consecutive_losses,"cooldown":self.cooldown_remaining}
