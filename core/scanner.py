"""
Scanner: Discovers active BTC 5-minute prediction markets on Polymarket.

Market slug pattern: btc-updown-5m-{UNIX_TIMESTAMP}
Markets resolve every 5 minutes on round timestamps.
"""

import logging
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

    def fetch_btc_minute_markets(self):
        """Find active BTC 5-min markets using the known slug pattern."""
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

        log.info(f"Found {len(unique)} active BTC 5-min markets")
        return unique

    def _fetch_by_slug_pattern(self):
        """Generate slugs based on btc-updown-5m-{timestamp} pattern."""
        markets = []
        now = int(time.time())
        current_window = (now // 300) * 300

        timestamps = [
            current_window - 600,
            current_window - 300,
            current_window,
            current_window + 300,
        ]

        for ts in timestamps:
            slug = f"btc-updown-5m-{ts}"
            market = self._fetch_market_by_slug(slug)
            if market:
                markets.append(market)

        return markets

    def _fetch_market_by_slug(self, slug):
        """Fetch a specific market by slug from Gamma API."""
        try:
            resp = self.session.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug, "limit": 1},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list) and len(data) > 0:
                return self._normalize_market(data[0])
            elif isinstance(data, dict) and data.get("id"):
                return self._normalize_market(data)
        except Exception as e:
            log.debug(f"Slug fetch failed for {slug}: {e}")
        return None

    def _search_gamma_api(self):
        """Broader search for BTC minute markets."""
        markets = []
        try:
            params = {
                "active": "true",
                "closed": "false",
                "limit": 20,
                "order": "endDate",
                "ascending": "true",
            }
            resp = self.session.get(f"{GAMMA_API}/markets", params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            for m in data if isinstance(data, list) else []:
                slug = m.get("slug", "")
                question = m.get("question", "").lower()
                is_btc_5m = (
                    slug.startswith("btc-updown-5m")
                    or ("bitcoin" in question and "5" in question and "minute" in question)
                    or ("btc" in question and ("above" in question or "below" in question))
                )
                if is_btc_5m:
                    market = self._normalize_market(m)
                    if market:
                        markets.append(market)
        except Exception as e:
            log.error(f"Gamma API search error: {e}")
        return markets

    def _normalize_market(self, raw):
        """Normalize Gamma API market to internal format."""
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

            return {
                "id": str(raw.get("conditionId", raw.get("id", ""))),
                "condition_id": raw.get("conditionId", ""),
                "question": raw.get("question", ""),
                "slug": raw.get("slug", ""),
                "end_time": raw.get("endDate", ""),
                "token_ids": tokens,
                "yes_price": yes_price,
                "no_price": no_price,
                "volume": raw.get("volume", 0),
                "liquidity": raw.get("liquidity", 0),
                "active": raw.get("active", True),
                "resolved": raw.get("resolved", False),
            }
        except Exception as e:
            log.debug(f"Normalize error: {e}")
            return None

    def get_order_book(self, token_id):
        """Fetch order book for a token (public endpoint, no auth)."""
        try:
            resp = self.session.get(
                f"{CLOB_API}/book",
                params={"token_id": token_id},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.debug(f"Order book error: {e}")
            return None

    def get_market_trades(self, condition_id):
        """Fetch recent trades for a market."""
        try:
            resp = self.session.get(
                f"{CLOB_API}/trades",
                params={"condition_id": condition_id},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.debug(f"Trades fetch error: {e}")
            return []

    def parse_book(self, book_data):
        """Parse order book into useful metrics."""
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

        depth = sum(
            float(o.get("size", 0))
            for o in bids + asks
            if abs(float(o.get("price", 0)) - mid) < 0.05
        )

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "mid": mid,
            "depth": depth,
            "n_bids": len(bids),
            "n_asks": len(asks),
        }

    def get_price_from_gamma(self, market):
        """Get current prices directly from Gamma data (no auth needed)."""
        return {
            "yes_price": market.get("yes_price"),
            "no_price": market.get("no_price"),
            "volume": market.get("volume", 0),
            "liquidity": market.get("liquidity", 0),
        }
