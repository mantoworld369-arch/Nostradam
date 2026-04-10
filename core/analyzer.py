"""
Analyzer: Detects microstructure inefficiencies in Polymarket odds.

Strategies:
1. Spread Compression — when spread narrows suddenly, a direction is forming
2. Mid-Price Mean Reversion — if mid swings >X% in 1-2 snapshots, fade it
3. Book Imbalance — if bid depth >> ask depth (or vice versa), lean that way
4. Stale Odds — if odds haven't moved despite volume, the market is mispriced
5. Momentum Cascade — rapid one-directional movement suggests continuation

Each signal returns: side (YES/NO), edge estimate, confidence, signal_type
"""

import logging
import numpy as np
from datetime import datetime, timezone

log = logging.getLogger("nostradam.analyzer")


class Signal:
    def __init__(self, side, edge, confidence, signal_type, meta=None):
        self.side = side            # "YES" or "NO"
        self.edge = edge            # estimated edge (0.0 - 1.0)
        self.confidence = confidence  # 0.0 - 1.0
        self.signal_type = signal_type
        self.meta = meta or {}

    def __repr__(self):
        return f"Signal({self.side}, edge={self.edge:.3f}, conf={self.confidence:.2f}, type={self.signal_type})"


class Analyzer:
    def __init__(self, cfg):
        self.cfg = cfg
        s = cfg["strategy"]
        self.min_edge = s["min_edge"]
        self.spread_threshold = s["spread_threshold"]
        self.momentum_threshold = s["momentum_threshold"]
        self.mean_reversion_window = s["mean_reversion_window"]

    def analyze(self, snapshots, current_book_yes, current_book_no):
        """
        Given a list of recent snapshots and current book state,
        return a list of Signals (may be empty).
        """
        signals = []

        if len(snapshots) < 3:
            return signals

        mids = [s["mid_yes"] for s in snapshots]
        spreads = [s["spread_yes"] for s in snapshots]
        depths_yes = [s.get("book_depth_yes", 0) for s in snapshots]
        depths_no = [s.get("book_depth_no", 0) for s in snapshots]

        cur_mid = mids[-1]
        cur_spread = spreads[-1]

        # --- Strategy 1: Mean Reversion ---
        sig = self._mean_reversion(mids, cur_mid, cur_spread)
        if sig:
            signals.append(sig)

        # --- Strategy 2: Book Imbalance ---
        if current_book_yes and current_book_no:
            sig = self._book_imbalance(current_book_yes, current_book_no, cur_mid)
            if sig:
                signals.append(sig)

        # --- Strategy 3: Momentum Cascade ---
        sig = self._momentum(mids, cur_spread)
        if sig:
            signals.append(sig)

        # --- Strategy 4: Spread Compression ---
        sig = self._spread_compression(spreads, mids, cur_spread)
        if sig:
            signals.append(sig)

        # --- Strategy 5: Stale Odds (volume without movement) ---
        sig = self._stale_odds(mids, snapshots)
        if sig:
            signals.append(sig)

        # Filter by minimum edge
        signals = [s for s in signals if s.edge >= self.min_edge]

        if signals:
            best = max(signals, key=lambda s: s.edge * s.confidence)
            log.info(f"Best signal: {best}")
            return [best]  # Only take the strongest signal per cycle

        return []

    def _mean_reversion(self, mids, cur_mid, cur_spread):
        """If odds swung hard recently, fade the move."""
        if len(mids) < 5:
            return None

        window = mids[-self.mean_reversion_window:]
        avg = np.mean(window[:-1])
        deviation = cur_mid - avg

        if abs(deviation) < self.min_edge:
            return None

        if cur_spread > self.spread_threshold:
            return None

        # Fade the deviation
        if deviation > 0:
            # YES is overpriced, buy NO
            side = "NO"
            edge = deviation * 0.6  # discount — not all deviation reverts
        else:
            # YES is underpriced, buy YES
            side = "YES"
            edge = abs(deviation) * 0.6

        confidence = min(abs(deviation) / 0.15, 1.0)  # scale confidence

        return Signal(side, edge, confidence, "mean_reversion", {
            "avg_mid": avg, "current_mid": cur_mid, "deviation": deviation
        })

    def _book_imbalance(self, book_yes, book_no, cur_mid):
        """If one side of the book is much deeper, lean that way."""
        depth_y = book_yes.get("depth", 0)
        depth_n = book_no.get("depth", 0)

        if depth_y == 0 and depth_n == 0:
            return None

        total = depth_y + depth_n
        if total < 100:  # too thin
            return None

        imbalance = (depth_y - depth_n) / total  # positive = more YES support

        if abs(imbalance) < 0.25:
            return None

        if imbalance > 0:
            # More bid depth on YES side → market leans YES
            fair_value_yes = cur_mid + imbalance * 0.1
            edge = fair_value_yes - cur_mid
            side = "YES"
        else:
            fair_value_no = (1 - cur_mid) + abs(imbalance) * 0.1
            edge = fair_value_no - (1 - cur_mid)
            side = "NO"

        edge = min(edge, 0.15)
        confidence = min(abs(imbalance), 1.0)

        return Signal(side, edge, confidence, "book_imbalance", {
            "depth_yes": depth_y, "depth_no": depth_n, "imbalance": imbalance
        })

    def _momentum(self, mids, cur_spread):
        """Detect rapid one-directional movement (continuation signal)."""
        if len(mids) < 4:
            return None

        recent = mids[-4:]
        diffs = [recent[i+1] - recent[i] for i in range(len(recent)-1)]

        # All diffs same direction and total move > threshold
        all_up = all(d > 0.005 for d in diffs)
        all_down = all(d < -0.005 for d in diffs)
        total_move = sum(diffs)

        if not (all_up or all_down):
            return None

        if abs(total_move) < self.momentum_threshold:
            return None

        if cur_spread > self.spread_threshold:
            return None

        if all_up:
            side = "YES"
            edge = abs(total_move) * 0.4  # momentum continuation discount
        else:
            side = "NO"
            edge = abs(total_move) * 0.4

        confidence = min(abs(total_move) / 0.2, 1.0)

        return Signal(side, edge, confidence, "momentum", {
            "diffs": diffs, "total_move": total_move
        })

    def _spread_compression(self, spreads, mids, cur_spread):
        """Spread narrowing after wide period suggests directional move incoming."""
        if len(spreads) < 6:
            return None

        avg_spread = np.mean(spreads[-6:-1])
        if avg_spread < 0.03:
            return None

        compression = avg_spread - cur_spread
        if compression < 0.02:
            return None

        # Direction hint: which side compressed?
        recent_mid_trend = mids[-1] - mids[-3] if len(mids) >= 3 else 0

        if recent_mid_trend > 0.01:
            side = "YES"
        elif recent_mid_trend < -0.01:
            side = "NO"
        else:
            return None  # No directional hint

        edge = compression * 0.5
        confidence = min(compression / 0.05, 1.0)

        return Signal(side, edge, confidence, "spread_compression", {
            "avg_spread": avg_spread, "cur_spread": cur_spread, "compression": compression
        })

    def _stale_odds(self, mids, snapshots):
        """Odds flat despite volume → potential misprice about to correct."""
        if len(mids) < 6:
            return None

        recent_mids = mids[-6:]
        mid_range = max(recent_mids) - min(recent_mids)

        # Odds are stale if range < 1%
        if mid_range > 0.01:
            return None

        # Check if there's been volume
        volumes = [s.get("volume", 0) for s in snapshots[-6:]]
        avg_vol = np.mean(volumes) if volumes else 0

        if avg_vol < self.cfg["strategy"]["volume_min"] * 0.1:
            return None

        # Stale odds with volume = tension building
        # Lean toward 50% (mean revert toward fair)
        cur_mid = mids[-1]
        if cur_mid > 0.55:
            side = "NO"
            edge = (cur_mid - 0.5) * 0.3
        elif cur_mid < 0.45:
            side = "YES"
            edge = (0.5 - cur_mid) * 0.3
        else:
            return None  # Already near fair

        confidence = 0.4  # low confidence — speculative
        return Signal(side, edge, confidence, "stale_odds", {
            "mid_range": mid_range, "avg_volume": avg_vol
        })
