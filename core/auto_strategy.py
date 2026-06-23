"""Daily/weekly long-only auto-trading strategy."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


AUTO_TRADE_TAG = "自动交易"


@dataclass
class AutoTradeSignal:
    symbol: str
    close: float
    atr: float
    bb_mid: float
    stop_price: float
    market_cap: float
    market_cap_source: dict[str, Any]
    quote_volume: float
    low_60d: float
    low_position_pct: float
    volume_ratio: float
    bandwidth: float
    bandwidth_ratio: float | None
    score: tuple[float, float, float]


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _bandwidth(boll: dict, idx: int) -> float:
    upper = float(boll["Upper Band"][idx])
    lower = float(boll["Lower Band"][idx])
    mid = float(boll["Middle Band"][idx])
    if mid <= 0:
        return math.nan
    return (upper - lower) / mid


def _has_enough_daily(sym: dict) -> bool:
    data = sym.get("1D", {}).get("data") or []
    boll = sym.get("1D", {}).get("bolling") or {}
    atr = sym.get("1D", {}).get("atr") or []
    return (
        len(data) >= 61
        and len(boll.get("Middle Band", [])) >= 5
        and len(atr) >= 15
    )


def evaluate_auto_trade_signal(
    symbol: str,
    sym: dict,
    market_cap_info: dict[str, Any] | None,
    min_market_cap: float = 5_000_000,
    max_market_cap: float = 1_000_000_000,
    min_quote_volume: float = 500_000,
    atr_min: float = 0.001,
    atr_stop_multi: float = 1.2,
    low_60d_min_pct: float = 0.01,
    low_60d_max_pct: float = 0.20,
) -> AutoTradeSignal | None:
    """Return a signal if the symbol matches the confirmed auto-trading rules."""
    if not _has_enough_daily(sym) or market_cap_info is None:
        return None

    market_cap = float(market_cap_info.get("market_cap") or 0)
    if not (min_market_cap < market_cap < max_market_cap):
        return None

    day = sym["1D"]
    data = day["data"]
    boll = day["bolling"]
    atr_values = day["atr"]

    close = float(data[-1][4])
    bb_mid = float(boll["Middle Band"][-1])
    atr = float(atr_values[-1])
    if not _finite(atr) or atr < atr_min:
        return None

    lows_60 = [float(x[3]) for x in data[-60:]]
    low_60d = min(lows_60)
    if low_60d <= 0:
        return None
    low_position_pct = (close - low_60d) / low_60d
    if not (low_60d_min_pct <= low_position_pct <= low_60d_max_pct):
        return None

    upper = boll["Upper Band"]
    lower = boll["Lower Band"]
    mid = boll["Middle Band"]
    for arr in (upper, lower, mid):
        if any(not _finite(arr[i]) for i in (-4, -3, -2, -1)):
            return None

    bandwidths = [_bandwidth(boll, i) for i in range(len(data))]
    if any(not _finite(bandwidths[i]) for i in (-4, -3, -2, -1)):
        return None

    converged = (
        upper[-4] >= upper[-3] >= upper[-2]
        and lower[-4] <= lower[-3] <= lower[-2]
        and bandwidths[-4] >= bandwidths[-3] >= bandwidths[-2]
    )
    opened = upper[-1] >= upper[-2] and lower[-1] <= lower[-2]
    if not (converged and opened):
        return None

    quote_volume = float(data[-1][6])
    if quote_volume < min_quote_volume:
        return None
    vol_ma20 = sum(float(x[6]) for x in data[-21:-1]) / 20
    if vol_ma20 <= 0 or quote_volume <= vol_ma20:
        return None
    volume_ratio = quote_volume / vol_ma20

    if close <= bb_mid:
        return None
    if not (mid[-1] > mid[-2] > mid[-3]):
        return None

    week = sym.get("1W", {})
    week_mid = (week.get("bolling") or {}).get("Middle Band") or []
    valid_week_mid = [x for x in week_mid if _finite(x)]
    if len(valid_week_mid) >= 2 and valid_week_mid[-1] < valid_week_mid[-2]:
        return None

    bw_valid = [x for x in bandwidths[-21:-1] if _finite(x)]
    bandwidth_ratio = None
    if bw_valid:
        avg_bw20 = sum(bw_valid) / len(bw_valid)
        if avg_bw20 > 0:
            bandwidth_ratio = bandwidths[-1] / avg_bw20

    stop_price = bb_mid - atr * atr_stop_multi
    if stop_price <= 0 or close <= stop_price:
        return None

    score = (
        low_position_pct,
        -volume_ratio,
        bandwidth_ratio if bandwidth_ratio is not None else bandwidths[-1],
    )
    return AutoTradeSignal(
        symbol=symbol,
        close=close,
        atr=atr,
        bb_mid=bb_mid,
        stop_price=stop_price,
        market_cap=market_cap,
        market_cap_source=market_cap_info,
        quote_volume=quote_volume,
        low_60d=low_60d,
        low_position_pct=low_position_pct,
        volume_ratio=volume_ratio,
        bandwidth=bandwidths[-1],
        bandwidth_ratio=bandwidth_ratio,
        score=score,
    )


def build_auto_trade_reason(signal: AutoTradeSignal) -> str:
    source = signal.market_cap_source
    return (
        "自动交易: 日K低位布林收敛开口 + 放量站上中轨 + 日/周趋势向上; "
        f"市值={signal.market_cap:,.0f}({source.get('id')}); "
        f"60日低点上方={signal.low_position_pct * 100:.2f}%; "
        f"成交额={signal.quote_volume:,.0f}; "
        f"ATR={signal.atr:.6g}; 止损={signal.stop_price:.6g}"
    )
