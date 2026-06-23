"""Average True Range calculation."""
from __future__ import annotations

import math


def calculate_atr(data: list, window: int = 14) -> list[float]:
    """Return ATR values aligned with the input K-line list."""
    if not data:
        return []

    true_ranges: list[float] = []
    prev_close: float | None = None
    for bar in data:
        high = float(bar[2])
        low = float(bar[3])
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
        prev_close = float(bar[4])

    atr: list[float] = [math.nan] * len(true_ranges)
    if window <= 0 or len(true_ranges) < window:
        return atr

    first = sum(true_ranges[:window]) / window
    atr[window - 1] = first
    prev_atr = first
    for i in range(window, len(true_ranges)):
        prev_atr = (prev_atr * (window - 1) + true_ranges[i]) / window
        atr[i] = prev_atr
    return atr
