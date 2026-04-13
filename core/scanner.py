"""Scanner v0.5"""
import logging, re, time, requests
from datetime import datetime, timezone
log = logging.getLogger("nostradam.scanner")
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

class MarketScanner:
    def __init__(self, cfg):
        self.cfg = cfg; self.known_markets = {}
        self.session = requests.Session()
        self.session.headers.update({"Content-Type":"application/json"})
        self.market_state = {"current":None,"next":None}

    def fetch_btc_minute_markets(self):
        markets = self._fetch_by_slug() + self._search_gamma()
        seen=set(); unique=[]
        for m in markets:
            k=m.get("id") or m.get("slug","")
            if k and k not in seen: seen.add(k); unique.append(m); self.known_markets[k]=m
        self._update_state(unique)
        log.info(f"Found {len(unique)} active BTC 5-min markets")
        return unique

    def _update_state(self, markets):
        now=datetime.now(timezone.utc); active=[]
        for m in markets:
            if m.get("resolved"): continue
            et=m.get("end_time","")
            if not et: continue
            try:
                end=datetime.fromisoformat(et.replace("Z","+00:00"))
                rem=(end-now).total_seconds()
                if rem>-60: active.append((rem,m))
            except: continue
        active.sort(key=lambda x:x[0])
        cur=nxt=None
        for rem,m in active:
            if rem>0 and not cur: cur=self._bld(m,rem)
            elif rem>0 and cur and not nxt: nxt=self._bld(m,rem)
        self.market_state={"current":cur,"next":nxt}

    def _bld(self, m, rem):
        return {"question":m.get("question",""),"slug":m.get("slug",""),
                "yes_price":m.get("yes_price"),"no_price":m.get("no_price"),
                "volume":m.get("volume",0),"end_time":m.get("end_time",""),
                "seconds_remaining":max(0,int(rem))}

    def _fetch_by_slug(self):
        markets=[]; now=int(time.time()); cw=(now//300)*300
        for ts in [cw-600,cw-300,cw,cw+300,cw+600]:
            m=self._get_slug(f"btc-updown-5m-{ts}")
            if m: markets.append(m)
        return markets

    def _get_slug(self, slug):
        try:
            r=self.session.get(f"{GAMMA_API}/markets",params={"slug":slug,"limit":1},timeout=10)
            r.raise_for_status(); d=r.json()
            if isinstance(d,list) and d: return self._norm(d[0])
        except: pass
        return None

    def _search_gamma(self):
        markets=[]
        try:
            r=self.session.get(f"{GAMMA_API}/markets",params={"active":"true","closed":"false","limit":20,"order":"endDate","ascending":"true"},timeout=10)
            r.raise_for_status()
            for m in (r.json() if isinstance(r.json(),list) else []):
                s=m.get("slug",""); q=m.get("question","").lower()
                if s.startswith("btc-updown-5m") or ("btc" in q and ("above" in q or "below" in q)):
                    nm=self._norm(m)
                    if nm: markets.append(nm)
        except Exception as e: log.error(f"Gamma err: {e}")
        return markets

    def _norm(self, raw):
        try:
            tokens=raw.get("clobTokenIds","")
            if isinstance(tokens,str): tokens=[t.strip().strip('"') for t in tokens.strip("[]").split(",") if t.strip()]
            elif isinstance(tokens,list): tokens=[str(t) for t in tokens]
            prices=raw.get("outcomePrices","")
            if isinstance(prices,str): prices=[p.strip().strip('"') for p in prices.strip("[]").split(",") if p.strip()]
            yp=float(prices[0]) if prices else None
            np_=float(prices[1]) if len(prices)>1 else None
            return {"id":str(raw.get("conditionId",raw.get("id",""))),"condition_id":raw.get("conditionId",""),
                    "question":raw.get("question",""),"slug":raw.get("slug",""),"end_time":raw.get("endDate",""),
                    "token_ids":tokens,"yes_price":yp,"no_price":np_,
                    "volume":float(raw.get("volume",0) or 0),"active":raw.get("active",True),"resolved":raw.get("resolved",False)}
        except: return None

    def get_order_book(self, tid):
        try:
            r=self.session.get(f"{CLOB_API}/book",params={"token_id":tid},timeout=10)
            r.raise_for_status(); return r.json()
        except: return None

    def parse_book(self, bd):
        if not bd: return None
        bids=bd.get("bids",[]); asks=bd.get("asks",[])
        if not bids and not asks: return None
        bids=sorted(bids,key=lambda x:float(x.get("price",0)),reverse=True)
        asks=sorted(asks,key=lambda x:float(x.get("price",0)))
        bb=float(bids[0]["price"]) if bids else 0; ba=float(asks[0]["price"]) if asks else 1
        sp=ba-bb; mid=(bb+ba)/2 if bids and asks else bb or ba
        depth=sum(float(o.get("size",0)) for o in bids+asks if abs(float(o.get("price",0))-mid)<0.05)
        return {"best_bid":bb,"best_ask":ba,"spread":sp,"mid":mid,"depth":depth}

    def fetch_resolution(self, mid):
        try:
            r=self.session.get(f"{GAMMA_API}/markets/{mid}",timeout=10)
            if r.ok:
                d=r.json()
                if d.get("resolved"):
                    p=d.get("outcomePrices","")
                    if isinstance(p,str): p=p.strip("[]").split(",")
                    if len(p)>=2: return "YES" if float(str(p[0]).strip().strip('"'))>0.5 else "NO"
        except: pass
        m=self.known_markets.get(mid,{}); slug=m.get("slug","")
        if slug:
            try:
                r=self.session.get(f"{GAMMA_API}/markets",params={"slug":slug,"limit":1},timeout=10)
                if r.ok:
                    d=r.json()
                    if isinstance(d,list) and d and d[0].get("resolved"):
                        p=d[0].get("outcomePrices","")
                        if isinstance(p,str): p=p.strip("[]").split(",")
                        if len(p)>=2: return "YES" if float(str(p[0]).strip().strip('"'))>0.5 else "NO"
            except: pass
        return None
