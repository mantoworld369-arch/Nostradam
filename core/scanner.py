"""Scanner v0.4: Proper BTC price, real volume, dual market display."""

import logging
import re
import time
import requests
from datetime import datetime, timezone

log = logging.getLogger("nostradam.scanner")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


class MarketScanner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.known_markets = {}
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.market_state = {
            "current": None,
            "next": None,
        }

    def fetch_btc_minute_markets(self):
        markets = []
        markets += self._fetch_by_slug_pattern()
        markets += self._search_gamma_api()

        seen = set()
        unique = []
        for m in markets:
            key = m.get("id") or m.get("slug", "")
            if key and key not in seen:
                seen.add(key)
                unique.append(m)
                self.known_markets[key] = m

        self._update_market_state(unique)
        log.info(f"Found {len(unique)} active BTC 5-min markets")
        return unique

    def _update_market_state(self, markets):
        """Track current and next market for dashboard."""
        now = datetime.now(timezone.utc)
        active = []

        for m in markets:
            if m.get("resolved"):
                continue
            end_time = m.get("end_time", "")
            if not end_time:
                continue
            try:
                end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                remaining = (end_dt - now).total_seconds()
                if remaining > -60:  # include recently ended (within 60s)
                    active.append((remaining, m))
            except (ValueError, AttributeError):
                continue

        active.sort(key=lambda x: x[0])

        current = None
        next_mkt = None

        for remaining, m in active:
            if remaining > 0 and current is None:
                current = self._build_state(m, remaining)
            elif remaining > 0 and current is not None and next_mkt is None:
                next_mkt = self._build_state(m, remaining)

        self.market_state["current"] = current
        self.market_state["next"] = next_mkt

    def _build_state(self, m, remaining):
        btc_target = self._extract_btc_price(m.get("question", ""))
        return {
            "btc_target": btc_target,
            "question": m.get("question", ""),
            "slug": m.get("slug", ""),
            "yes_price": m.get("yes_price"),
            "no_price": m.get("no_price"),
            "volume": m.get("volume", 0),
            "liquidity": m.get("liquidity", 0),
            "end_time": m.get("end_time", ""),
            "seconds_remaining": max(0, int(remaining)),
        }

    def _extract_btc_price(self, question):
        """Extract BTC price from question text."""
        match = re.search(r'\$[\d,]+(?:\.\d+)?', question)
        if match:
            try:
                return float(match.group().replace('$', '').replace(',', ''))
            except ValueError:
                pass
        # Try plain number patterns like "71806"
        match = re.search(r'(\d{4,6}(?:\.\d+)?)', question)
        if match:
            val = float(match.group())
            if 10000 < val < 200000:  # reasonable BTC range
                return val
        return None

    def _fetch_by_slug_pattern(self):
        markets = []
        now = int(time.time())
        current_window = (now // 300) * 300
        for ts in [current_window - 600, current_window - 300, current_window, current_window + 300, current_window + 600]:
            slug = f"btc-updown-5m-{ts}"
            m = self._fetch_market_by_slug(slug)
            if m:
                markets.append(m)
        return markets

    def _fetch_market_by_slug(self, slug):
        try:
            resp = self.session.get(f"{GAMMA_API}/markets", params={"slug": slug, "limit": 1}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return self._normalize_market(data[0])
            elif isinstance(data, dict) and data.get("id"):
                return self._normalize_market(data)
        except Exception as e:
            log.debug(f"Slug fetch failed {slug}: {e}")
        return None

    def _search_gamma_api(self):
        markets = []
        try:
            resp = self.session.get(f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false", "limit": 20, "order": "endDate", "ascending": "true"}, timeout=10)
            resp.raise_for_status()
            for m in resp.json() if isinstance(resp.json(), list) else []:
                slug = m.get("slug", "")
                q = m.get("question", "").lower()
                if slug.startswith("btc-updown-5m") or ("btc" in q and ("above" in q or "below" in q)):
                    nm = self._normalize_market(m)
                    if nm:
                        markets.append(nm)
        except Exception as e:
            log.error(f"Gamma search error: {e}")
        return markets

    def _normalize_market(self, raw):
        try:
            tokens = raw.get("clobTokenIds", "")
            if isinstance(tokens, str):
                tokens = [t.strip().strip('"') for t in tokens.strip("[]").split(",") if t.strip()]
            elif isinstance(tokens, list):
                tokens = [str(t) for t in tokens]

            prices = raw.get("outcomePrices", "")
            if isinstance(prices, str):
                prices = [p.strip().strip('"') for p in prices.strip("[]").split(",") if p.strip()]

            yes_price = float(prices[0]) if prices else None
            no_price = float(prices[1]) if len(prices) > 1 else None

            # Real volume from Gamma API
            volume = 0
            try:
                volume = float(raw.get("volume", 0) or 0)
            except (TypeError, ValueError):
                pass

            liquidity = 0
            try:
                liquidity = float(raw.get("liquidity", 0) or 0)
            except (TypeError, ValueError):
                pass

            return {
                "id": str(raw.get("conditionId", raw.get("id", ""))),
                "condition_id": raw.get("conditionId", ""),
                "question": raw.get("question", ""),
                "slug": raw.get("slug", ""),
                "end_time": raw.get("endDate", ""),
                "token_ids": tokens,
                "yes_price": yes_price,
                "no_price": no_price,
                "volume": volume,
                "liquidity": liquidity,
                "active": raw.get("active", True),
                "resolved": raw.get("resolved", False),
            }
        except Exception as e:
            log.debug(f"Normalize error: {e}")
            return None

    def get_order_book(self, token_id):
        try:
            resp = self.session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.debug(f"Book error: {e}")
            return None

    def parse_book(self, book_data):
        if not book_data:
            return None
        bids = book_data.get("bids", [])
        asks = book_data.get("asks", [])
        if not bids and not asks:
            return None
        bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
        asks = sorted(asks, key=lambda x: float(x.get("price", 0)))
        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 1
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2 if (bids and asks) else best_bid or best_ask
        depth = sum(float(o.get("size", 0)) for o in bids + asks if abs(float(o.get("price", 0)) - mid) < 0.05)
        return {"best_bid": best_bid, "best_ask": best_ask, "spread": spread, "mid": mid, "depth": depth, "n_bids": len(bids), "n_asks": len(asks)}

    def fetch_resolution(self, market_id):
        """Fetch market resolution. Try multiple approaches."""
        # Try by condition ID
        outcome = self._try_gamma_resolution(market_id)
        if outcome:
            return outcome

        # Try by slug
        market = self.known_markets.get(market_id, {})
        slug = market.get("slug", "")
        if slug:
            outcome = self._try_gamma_slug_resolution(slug)
            if outcome:
                return outcome

        return None

    def _try_gamma_resolution(self, market_id):
        try:
            resp = self.session.get(f"{GAMMA_API}/markets/{market_id}", timeout=10)
            if resp.ok:
                data = resp.json()
                return self._parse_resolution(data)
        except Exception:
            pass
        return None

    def _try_gamma_slug_resolution(self, slug):
        try:
            resp = self.session.get(f"{GAMMA_API}/markets", params={"slug": slug, "limit": 1}, timeout=10)
            if resp.ok:
                data = resp.json()
                if isinstance(data, list) and data:
                    return self._parse_resolution(data[0])
        except Exception:
            pass
        return None

    def _parse_resolution(self, data):
        if not data.get("resolved"):
            return None
        prices = data.get("outcomePrices", "")
        if isinstance(prices, str):
            prices = prices.strip("[]").split(",")
        if isinstance(prices, list) and len(prices) >= 2:
            try:
                yes_val = float(str(prices[0]).strip().strip('"'))
                return "YES" if yes_val > 0.5 else "NO"
            except ValueError:
                pass
        return None
