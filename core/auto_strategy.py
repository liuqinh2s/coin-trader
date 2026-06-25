"""Daily/weekly long-only auto-trading strategy."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


AUTO_TRADE_TAG = "自动交易"
AUTO_TRADE_FILTER_TAGS = [
    "小市值",
    "放量",
    "价格>中轨",
    "趋势向上",
    "成交额合格",
]


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


def _has_enough_daily(sym: dict) -> bool:
    data = sym.get("1D", {}).get("data") or []
    boll = sym.get("1D", {}).get("bolling") or {}
    atr = sym.get("1D", {}).get("atr") or []
    return (
        len(data) >= 61
        and len(boll.get("Middle Band", [])) >= 5
        and len(atr) >= 15
    )


def evaluate_auto_trade_conditions(
    sym: dict,
    market_cap_info: dict[str, Any] | None,
    min_market_cap: float = 5_000_000,
    max_market_cap: float = 1_000_000_000,
    min_quote_volume: float = 500_000,
) -> dict[str, bool]:
    """Evaluate each visible auto-trading filter tag independently."""
    result = {tag: False for tag in AUTO_TRADE_FILTER_TAGS}

    market_cap = float((market_cap_info or {}).get("market_cap") or 0)
    result["小市值"] = min_market_cap < market_cap < max_market_cap

    if not _has_enough_daily(sym):
        return result

    day = sym["1D"]
    data = day["data"]
    boll = day["bolling"]
    mid = boll["Middle Band"]

    close = float(data[-1][4])

    quote_volume = float(data[-1][6])
    result["成交额合格"] = quote_volume >= min_quote_volume
    vol_ma20 = sum(float(x[6]) for x in data[-21:-1]) / 20
    result["放量"] = vol_ma20 > 0 and quote_volume > vol_ma20

    result["价格>中轨"] = _finite(mid[-1]) and close > float(mid[-1])

    daily_up = _finite(mid[-1]) and _finite(mid[-2]) and _finite(mid[-3]) and mid[-1] > mid[-2] > mid[-3]
    week = sym.get("1W", {})
    week_mid = (week.get("bolling") or {}).get("Middle Band") or []
    valid_week_mid = [x for x in week_mid if _finite(x)]
    week_ok = len(valid_week_mid) < 2 or valid_week_mid[-1] >= valid_week_mid[-2]
    result["趋势向上"] = daily_up and week_ok

    return result


def evaluate_auto_trade_signal(
    symbol: str,
    sym: dict,
    market_cap_info: dict[str, Any] | None,
    min_market_cap: float = 5_000_000,
    max_market_cap: float = 1_000_000_000,
    min_quote_volume: float = 500_000,
    atr_min: float = 0.001,
    atr_stop_multi: float = 1.2,
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
    mid = boll["Middle Band"]

    close = float(data[-1][4])
    bb_mid = float(mid[-1])
    atr = float(atr_values[-1])
    if not _finite(atr) or atr < atr_min:
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
    if not (_finite(mid[-1]) and _finite(mid[-2]) and _finite(mid[-3]) and mid[-1] > mid[-2] > mid[-3]):
        return None

    week = sym.get("1W", {})
    week_mid = (week.get("bolling") or {}).get("Middle Band") or []
    valid_week_mid = [x for x in week_mid if _finite(x)]
    if len(valid_week_mid) >= 2 and valid_week_mid[-1] < valid_week_mid[-2]:
        return None

    lows_60 = [float(x[3]) for x in data[-60:]]
    low_60d = min(lows_60) if lows_60 else 0

    stop_price = bb_mid - atr * atr_stop_multi
    if stop_price <= 0 or close <= stop_price:
        return None

    low_position_pct = (close - low_60d) / low_60d if low_60d > 0 else 0
    score = (
        low_position_pct,
        -volume_ratio,
        0,
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
        bandwidth=0,
        bandwidth_ratio=None,
        score=score,
    )


def build_auto_trade_reason(signal: AutoTradeSignal) -> str:
    source = signal.market_cap_source
    return (
        "自动交易: 放量站上中轨 + 日/周趋势向上; "
        f"市值={signal.market_cap:,.0f}({source.get('id')}); "
        f"成交额={signal.quote_volume:,.0f}; "
        f"ATR={signal.atr:.6g}; 止损={signal.stop_price:.6g}"
    )
