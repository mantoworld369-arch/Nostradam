"""Analyzer v0.5.1: 7 strategies + side bias from historical performance."""
import logging, numpy as np
log = logging.getLogger("nostradam.analyzer")

class Signal:
    def __init__(self, side, edge, confidence, signal_type, meta=None):
        self.side=side; self.edge=edge; self.confidence=confidence
        self.signal_type=signal_type; self.meta=meta or {}
    def __repr__(self): return f"Signal({self.side}, edge={self.edge:.3f}, conf={self.confidence:.2f}, {self.signal_type})"

class Analyzer:
    def __init__(self, cfg):
        self.cfg=cfg; self.update_params(cfg)

    def update_params(self, cfg):
        s=cfg["strategy"]
        self.min_edge=s["min_edge"]; self.spread_threshold=s["spread_threshold"]
        self.momentum_threshold=s["momentum_threshold"]; self.mr_window=s["mean_reversion_window"]
        self.enabled=s.get("enabled_signals",["trend_follow","contrarian","book_imbalance","momentum","spread_compression","volatility_spike","odds_divergence"])
        # Side bias: >1.0 favors YES, <1.0 favors NO, 1.0 = neutral
        self.side_bias=s.get("side_bias",1.0)

    def analyze(self, snapshots, book_yes, book_no):
        if len(snapshots)<3: return []
        mids=[s["mid_yes"] for s in snapshots]
        spreads=[s["spread_yes"] for s in snapshots]
        cm=mids[-1]; cs=spreads[-1]
        signals=[]

        if "trend_follow" in self.enabled:
            s=self._trend_follow(mids,cm,cs)
            if s: signals.append(s)
        if "contrarian" in self.enabled:
            s=self._contrarian(mids,cm,cs)
            if s: signals.append(s)
        if "book_imbalance" in self.enabled and book_yes and book_no:
            s=self._book_imbalance(book_yes,book_no,cm)
            if s: signals.append(s)
        if "momentum" in self.enabled:
            s=self._momentum(mids,cs)
            if s: signals.append(s)
        if "spread_compression" in self.enabled:
            s=self._spread_compression(spreads,mids,cs)
            if s: signals.append(s)
        if "volatility_spike" in self.enabled:
            s=self._volatility_spike(mids,cm,cs)
            if s: signals.append(s)
        if "odds_divergence" in self.enabled:
            s=self._odds_divergence(snapshots,cm)
            if s: signals.append(s)

        # Apply side bias — scale confidence based on historical side performance
        for s in signals:
            s.confidence = self._apply_side_bias(s.side, s.confidence)

        signals=[s for s in signals if s.edge>=self.min_edge]
        if signals:
            best=max(signals,key=lambda s:s.edge*s.confidence)
            log.info(f"Best signal: {best} (side_bias={self.side_bias:.2f})")
            return [best]
        return []

    def _apply_side_bias(self, side, confidence):
        """Adjust confidence based on side bias.
        side_bias > 1.0 = YES wins more -> boost YES, penalize NO
        side_bias < 1.0 = NO wins more -> boost NO, penalize YES
        side_bias = 1.0 = neutral
        """
        if side == "YES":
            return confidence * self.side_bias
        else:  # NO
            # Inverse: if side_bias=0.5 (NO favored), NO gets 1/0.5=2.0x boost
            return confidence * (1.0 / self.side_bias) if self.side_bias > 0 else confidence * 2.0

    def _trend_follow(self, mids, cm, cs):
        """Follow the deviation direction (deviation UP -> YES)."""
        if len(mids)<5: return None
        avg=np.mean(mids[-self.mr_window:-1]); dev=cm-avg
        if abs(dev)<self.min_edge or cs>self.spread_threshold: return None
        side="YES" if dev>0 else "NO"
        return Signal(side,abs(dev)*0.6,min(abs(dev)/0.15,1.0),"trend_follow",{"avg":avg,"mid":cm,"dev":dev})

    def _contrarian(self, mids, cm, cs):
        """Fade the deviation (deviation UP -> NO). Opposite of trend_follow."""
        if len(mids)<5: return None
        avg=np.mean(mids[-self.mr_window:-1]); dev=cm-avg
        if abs(dev)<self.min_edge*1.5 or cs>self.spread_threshold: return None  # Higher threshold
        side="NO" if dev>0 else "YES"
        return Signal(side,abs(dev)*0.5,min(abs(dev)/0.2,1.0),"contrarian",{"avg":avg,"mid":cm,"dev":dev})

    def _book_imbalance(self, by, bn, cm):
        dy=by.get("depth",0); dn=bn.get("depth",0); total=dy+dn
        if total<100: return None
        imb=(dy-dn)/total
        if abs(imb)<0.25: return None
        side="YES" if imb>0 else "NO"
        return Signal(side,min(abs(imb)*0.1,0.15),min(abs(imb),1.0),"book_imbalance",{"dy":dy,"dn":dn,"imb":imb})

    def _momentum(self, mids, cs):
        if len(mids)<4: return None
        recent=mids[-4:]; diffs=[recent[i+1]-recent[i] for i in range(3)]
        au=all(d>0.005 for d in diffs); ad=all(d<-0.005 for d in diffs)
        tm=sum(diffs)
        if not(au or ad) or abs(tm)<self.momentum_threshold or cs>self.spread_threshold: return None
        return Signal("YES" if au else "NO",abs(tm)*0.4,min(abs(tm)/0.2,1.0),"momentum",{"tm":tm})

    def _spread_compression(self, spreads, mids, cs):
        if len(spreads)<6: return None
        avg_sp=np.mean(spreads[-6:-1])
        if avg_sp<0.03: return None
        comp=avg_sp-cs
        if comp<0.02: return None
        trend=mids[-1]-mids[-3] if len(mids)>=3 else 0
        if trend>0.01: side="YES"
        elif trend<-0.01: side="NO"
        else: return None
        return Signal(side,comp*0.5,min(comp/0.05,1.0),"spread_compression",{"comp":comp})

    def _volatility_spike(self, mids, cm, cs):
        """High recent volatility = opportunity for mean reversion to center."""
        if len(mids)<8: return None
        recent=mids[-8:]
        vol=np.std(recent)
        if vol<0.03: return None  # Not volatile enough
        # After high vol, lean toward 50%
        if cm>0.60: side,edge="NO",(cm-0.5)*0.4
        elif cm<0.40: side,edge="YES",(0.5-cm)*0.4
        else: return None
        return Signal(side,edge,min(vol/0.1,1.0),"volatility_spike",{"vol":vol,"mid":cm})

    def _odds_divergence(self, snapshots, cm):
        """YES+NO prices should sum to ~1.0. If they don't, arbitrage exists."""
        if len(snapshots)<2: return None
        s=snapshots[-1]
        bid_y=s.get("best_bid_yes",0); ask_y=s.get("best_ask_yes",1)
        bid_n=s.get("best_bid_no",0); ask_n=s.get("best_ask_no",1)
        # If best_bid_yes + best_bid_no > 1.0, both sides overpriced (sell opportunity)
        # If best_ask_yes + best_ask_no < 1.0, both sides underpriced (buy opportunity)
        ask_sum=ask_y+ask_n
        if ask_sum<0.96:  # Can buy both for <$1, guaranteed profit
            # Buy the cheaper side
            if ask_y<ask_n:
                return Signal("YES",1.0-ask_sum,0.8,"odds_divergence",{"ask_sum":ask_sum})
            else:
                return Signal("NO",1.0-ask_sum,0.8,"odds_divergence",{"ask_sum":ask_sum})
        bid_sum=bid_y+bid_n
        if bid_sum>1.04:  # Overbid — one side must be wrong
            if bid_y>bid_n:
                return Signal("NO",(bid_sum-1.0)*0.5,0.6,"odds_divergence",{"bid_sum":bid_sum})
            else:
                return Signal("YES",(bid_sum-1.0)*0.5,0.6,"odds_divergence",{"bid_sum":bid_sum})
        return None
