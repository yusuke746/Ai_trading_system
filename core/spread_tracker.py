"""
core/spread_tracker.py — 適応型スプレッド上限管理

直近N時間のスプレッドサンプルを蓄積し、
p95 × multiplier でリアルタイムに上限を自動算出する。

固定ハードMAXで天井クランプし、ニュース時の暴走を防止。
ウォームアップ中（サンプル不足）はconfig固定値にフォールバック。
"""

import logging
import time
from collections import defaultdict, deque
from typing import NamedTuple

from config import CONFIG

logger = logging.getLogger(__name__)


class SpreadSample(NamedTuple):
    timestamp: float  # time.time()
    spread_points: float


class SpreadTracker:
    """銘柄ごとのスプレッド履歴を管理し、適応型上限を算出する"""

    def __init__(self):
        # 銘柄ごとのサンプル保存（dequeで自動サイズ制限）
        max_samples = CONFIG.SPREAD_HISTORY_HOURS * 60  # 1分1サンプル想定
        self._history: dict[str, deque[SpreadSample]] = defaultdict(
            lambda: deque(maxlen=max_samples)
        )

    def record(self, symbol: str, spread_points: float) -> None:
        """スプレッドサンプルを記録"""
        self._history[symbol].append(
            SpreadSample(timestamp=time.time(), spread_points=spread_points)
        )

    def get_limit(self, symbol: str) -> float:
        """
        適応型スプレッド上限を返す。

        1. サンプル数 < MIN_SAMPLES → config固定値（ウォームアップ中）
        2. 直近HISTORY_HOURS内のサンプルでp95を計算
        3. p95 × multiplier を算出
        4. min(算出値, HARD_MAX) でクランプ
        """
        samples = self._get_valid_samples(symbol)

        if len(samples) < CONFIG.SPREAD_MIN_SAMPLES:
            # ウォームアップ中は固定値フォールバック
            return float(CONFIG.SPREAD_LIMITS_POINTS.get(symbol, 50))

        # p95計算（numpy不要の手動実装）
        sorted_spreads = sorted(s.spread_points for s in samples)
        idx = int(len(sorted_spreads) * CONFIG.SPREAD_ADAPTIVE_PERCENTILE / 100)
        idx = min(idx, len(sorted_spreads) - 1)
        p95 = sorted_spreads[idx]

        adaptive_limit = p95 * CONFIG.SPREAD_ADAPTIVE_MULTIPLIER
        hard_max = CONFIG.SPREAD_HARD_MAX_POINTS.get(symbol, 100)

        return min(adaptive_limit, hard_max)

    def get_stats(self, symbol: str) -> dict:
        """ステータスAPI・デバッグ用の統計情報"""
        samples = self._get_valid_samples(symbol)
        n = len(samples)

        if n == 0:
            return {
                "symbol": symbol,
                "samples": 0,
                "current_limit": CONFIG.SPREAD_LIMITS_POINTS.get(symbol, 50),
                "mode": "fixed_fallback",
            }

        spreads = [s.spread_points for s in samples]
        sorted_spreads = sorted(spreads)

        p50_idx = min(int(n * 0.50), n - 1)
        p95_idx = min(int(n * 0.95), n - 1)

        current_limit = self.get_limit(symbol)
        is_warmup = n < CONFIG.SPREAD_MIN_SAMPLES
        hard_max = CONFIG.SPREAD_HARD_MAX_POINTS.get(symbol, 100)

        return {
            "symbol": symbol,
            "samples": n,
            "mode": "warmup" if is_warmup else "adaptive",
            "current_limit": round(current_limit, 1),
            "hard_max": hard_max,
            "p50": round(sorted_spreads[p50_idx], 1),
            "p95": round(sorted_spreads[p95_idx], 1),
            "min": round(sorted_spreads[0], 1),
            "max": round(sorted_spreads[-1], 1),
            "latest": round(spreads[-1], 1) if spreads else 0,
        }

    def _get_valid_samples(self, symbol: str) -> list[SpreadSample]:
        """有効期間内のサンプルだけを返す"""
        cutoff = time.time() - CONFIG.SPREAD_HISTORY_HOURS * 3600
        return [s for s in self._history[symbol] if s.timestamp >= cutoff]
