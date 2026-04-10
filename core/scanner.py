"""
Scanner: Discovers active BTC 5-minute prediction markets on Polymarket.

Polymarket's BTC minute markets follow patterns like:
  "Will Bitcoin be above $XXXXX at HH:MM UTC?"
  
We search the CLOB API for active markets matching BTC/Bitcoin + minute/5-min patterns.
"""

import logging
import time
import requests
from datetime import datetime, timezone

log = logging.getLogger("nostradam.scanner")

# Polymarket APIs
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


class MarketScanner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.api_key = cfg["api"]["key"]
        self.known_markets = {}  # id -> market dict
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
        })
        if self.api_key:
            self.session.headers["POLY_API_KEY"] = self.api_key

    def fetch_btc_minute_markets(self):
        """Search for active BTC 5-minute prediction markets."""
        markets = []
        try:
            # Search via Gamma API (public, no auth needed)
            # BTC minute markets typically have "Bitcoin" + price target + time
            params = {
                "active": "true",
                "closed": "false",
                "limit": 50,
                "order": "endDate",
                "ascending": "true",
                "tag": "crypto",
            }
            resp = self.session.get(f"{GAMMA_API}/markets", params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            for m in data:
                q = (m.get("question", "") + " " + m.get("description", "")).lower()
                # Filter for BTC 5-minute / minute markets
                is_btc = any(kw in q for kw in ["bitcoin", "btc"])
                is_minute = any(kw in q for kw in [
                    "5 minute", "5-minute", "5min",
                    "at ", ":00", ":05", ":10", ":15", ":20", ":25", ":30", ":35", ":40", ":45", ":50", ":55"
                ])
                if is_btc and is_minute:
                    market = self._normalize_market(m)
                    if market:
                        markets.append(market)

            # Also try CLOB direct search
            markets += self._search_clob_markets()

        except Exception as e:
            log.error(f"Scanner error: {e}")

        # Deduplicate
        seen = set()
        unique = []
        for m in markets:
            if m["id"] not in seen:
                seen.add(m["id"])
                unique.append(m)
                self.known_markets[m["id"]] = m

        log.info(f"Found {len(unique)} active BTC minute markets")
        return unique

    def _search_clob_markets(self):
        """Search CLOB API directly for markets."""
        markets = []
        try:
            # The CLOB API /markets endpoint
            resp = self.session.get(
                f"{CLOB_API}/markets",
                params={"next_cursor": "MA=="},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()

            for m in data.get("data", data) if isinstance(data, dict) else data:
                q = m.get("question", "").lower()
                is_btc = "bitcoin" in q or "btc" in q
                is_short_term = any(kw in q for kw in ["minute", "5 min", "above", "below"])
                if is_btc and is_short_term:
                    market = self._normalize_clob_market(m)
                    if market:
                        markets.append(market)

        except Exception as e:
            log.debug(f"CLOB search error: {e}")
        return markets

    def _normalize_market(self, raw):
        """Normalize Gamma API market to our format."""
        try:
            tokens = raw.get("clobTokenIds", "")
            if isinstance(tokens, str):
                tokens = [t.strip() for t in tokens.strip("[]").split(",") if t.strip()]

            return {
                "id": str(raw.get("id", raw.get("conditionId", ""))),
                "condition_id": raw.get("conditionId", ""),
                "question": raw.get("question", ""),
                "slug": raw.get("slug", ""),
                "end_time": raw.get("endDate", ""),
                "token_ids": tokens,
                "outcome_yes": raw.get("outcomePrices", ""),
                "active": raw.get("active", True),
            }
        except Exception as e:
            log.debug(f"Normalize error: {e}")
            return None

    def _normalize_clob_market(self, raw):
        """Normalize CLOB API market."""
        try:
            tokens = raw.get("tokens", [])
            token_ids = [t.get("token_id", "") for t in tokens] if tokens else []

            return {
                "id": raw.get("condition_id", str(raw.get("id", ""))),
                "condition_id": raw.get("condition_id", ""),
                "question": raw.get("question", ""),
                "slug": raw.get("market_slug", ""),
                "end_time": raw.get("end_date_iso", ""),
                "token_ids": token_ids,
                "active": raw.get("active", True),
            }
        except Exception as e:
            log.debug(f"CLOB normalize error: {e}")
            return None

    def get_order_book(self, token_id):
        """Fetch order book for a specific token (YES or NO side)."""
        try:
            resp = self.session.get(
                f"{CLOB_API}/book",
                params={"token_id": token_id},
                timeout=10
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"Order book fetch error for {token_id}: {e}")
            return None

    def get_market_trades(self, condition_id):
        """Fetch recent trades for a market."""
        try:
            resp = self.session.get(
                f"{CLOB_API}/trades",
                params={"condition_id": condition_id},
                timeout=10
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

        if not bids or not asks:
            return None

        # Sort: bids descending, asks ascending
        bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
        asks = sorted(asks, key=lambda x: float(x.get("price", 0)))

        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 1
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2

        # Book depth (total size within 5% of mid)
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
