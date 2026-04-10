"""
Analyzer: Detects microstructure inefficiencies in Polymarket odds.

Strategies:
1. Mean Reversion — fade large odds swings
2. Book Imbalance — lean toward deeper side
3. Momentum Cascade — ride rapid directional movement
4. Spread Compression — narrowing spread signals incoming move
5. Stale Odds — flat odds with volume = tension
"""

import logging
import numpy as np

log = logging.getLogger("nostradam.analyzer")


class Signal:
    def __init__(self, side, edge, confidence, signal_type, meta=None):
        self.side = side
        self.edge = edge
        self.confidence = confidence
        self.signal_type = signal_type
        self.meta = meta or {}

    def __repr__(self):
        return f"Signal({self.side}, edge={self.edge:.3f}, conf={self.confidence:.2f}, type={self.signal_type})"


class Analyzer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.update_params(cfg)

    def update_params(self, cfg):
        """Update from config (called after optimization)."""
        s = cfg["strategy"]
        self.min_edge = s["min_edge"]
        self.spread_threshold = s["spread_threshold"]
        self.momentum_threshold = s["momentum_threshold"]
        self.mean_reversion_window = s["mean_reversion_window"]
        self.enabled_signals = s.get("enabled_signals", [
            "mean_reversion", "book_imbalance", "momentum",
            "spread_compression", "stale_odds"
        ])

    def analyze(self, snapshots, current_book_yes, current_book_no):
        signals = []
        if len(snapshots) < 3:
            return signals

        mids = [s["mid_yes"] for s in snapshots]
        spreads = [s["spread_yes"] for s in snapshots]

        cur_mid = mids[-1]
        cur_spread = spreads[-1]

        if "mean_reversion" in self.enabled_signals:
            sig = self._mean_reversion(mids, cur_mid, cur_spread)
            if sig:
                signals.append(sig)

        if "book_imbalance" in self.enabled_signals and current_book_yes and current_book_no:
            sig = self._book_imbalance(current_book_yes, current_book_no, cur_mid)
            if sig:
                signals.append(sig)

        if "momentum" in self.enabled_signals:
            sig = self._momentum(mids, cur_spread)
            if sig:
                signals.append(sig)

        if "spread_compression" in self.enabled_signals:
            sig = self._spread_compression(spreads, mids, cur_spread)
            if sig:
                signals.append(sig)

        if "stale_odds" in self.enabled_signals:
            sig = self._stale_odds(mids, snapshots)
            if sig:
                signals.append(sig)

        signals = [s for s in signals if s.edge >= self.min_edge]

        if signals:
            best = max(signals, key=lambda s: s.edge * s.confidence)
            log.info(f"Best signal: {best}")
            return [best]

        return []

    def _mean_reversion(self, mids, cur_mid, cur_spread):
        if len(mids) < 5:
            return None
        window = mids[-self.mean_reversion_window:]
        avg = np.mean(window[:-1])
        deviation = cur_mid - avg

        if abs(deviation) < self.min_edge or cur_spread > self.spread_threshold:
            return None

        if deviation > 0:
            side, edge = "NO", deviation * 0.6
        else:
            side, edge = "YES", abs(deviation) * 0.6

        confidence = min(abs(deviation) / 0.15, 1.0)
        return Signal(side, edge, confidence, "mean_reversion",
                      {"avg_mid": avg, "current_mid": cur_mid, "deviation": deviation})

    def _book_imbalance(self, book_yes, book_no, cur_mid):
        depth_y = book_yes.get("depth", 0)
        depth_n = book_no.get("depth", 0)
        total = depth_y + depth_n
        if total < 100:
            return None

        imbalance = (depth_y - depth_n) / total
        if abs(imbalance) < 0.25:
            return None

        if imbalance > 0:
            edge = min(imbalance * 0.1, 0.15)
            side = "YES"
        else:
            edge = min(abs(imbalance) * 0.1, 0.15)
            side = "NO"

        confidence = min(abs(imbalance), 1.0)
        return Signal(side, edge, confidence, "book_imbalance",
                      {"depth_yes": depth_y, "depth_no": depth_n, "imbalance": imbalance})

    def _momentum(self, mids, cur_spread):
        if len(mids) < 4:
            return None
        recent = mids[-4:]
        diffs = [recent[i+1] - recent[i] for i in range(len(recent)-1)]

        all_up = all(d > 0.005 for d in diffs)
        all_down = all(d < -0.005 for d in diffs)
        total_move = sum(diffs)

        if not (all_up or all_down) or abs(total_move) < self.momentum_threshold:
            return None
        if cur_spread > self.spread_threshold:
            return None

        side = "YES" if all_up else "NO"
        edge = abs(total_move) * 0.4
        confidence = min(abs(total_move) / 0.2, 1.0)
        return Signal(side, edge, confidence, "momentum",
                      {"diffs": diffs, "total_move": total_move})

    def _spread_compression(self, spreads, mids, cur_spread):
        if len(spreads) < 6:
            return None
        avg_spread = np.mean(spreads[-6:-1])
        if avg_spread < 0.03:
            return None

        compression = avg_spread - cur_spread
        if compression < 0.02:
            return None

        recent_mid_trend = mids[-1] - mids[-3] if len(mids) >= 3 else 0
        if recent_mid_trend > 0.01:
            side = "YES"
        elif recent_mid_trend < -0.01:
            side = "NO"
        else:
            return None

        edge = compression * 0.5
        confidence = min(compression / 0.05, 1.0)
        return Signal(side, edge, confidence, "spread_compression",
                      {"avg_spread": avg_spread, "cur_spread": cur_spread, "compression": compression})

    def _stale_odds(self, mids, snapshots):
        if len(mids) < 6:
            return None
        recent_mids = mids[-6:]
        mid_range = max(recent_mids) - min(recent_mids)
        if mid_range > 0.01:
            return None

        volumes = [s.get("volume", 0) for s in snapshots[-6:]]
        avg_vol = np.mean(volumes) if volumes else 0
        if avg_vol < self.cfg["strategy"]["volume_min"] * 0.1:
            return None

        cur_mid = mids[-1]
        if cur_mid > 0.55:
            side, edge = "NO", (cur_mid - 0.5) * 0.3
        elif cur_mid < 0.45:
            side, edge = "YES", (0.5 - cur_mid) * 0.3
        else:
            return None

        return Signal(side, edge, 0.4, "stale_odds",
                      {"mid_range": mid_range, "avg_volume": avg_vol})
