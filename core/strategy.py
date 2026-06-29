"""
策略模块：趋势判断（多周期）
"""
from __future__ import annotations


# =============================================================================
#  各周期趋势判断
# =============================================================================

def is_15m_trend_up(sym: dict, tf: str) -> bool:
    """15 分钟多头: 中轨上行 + DIF > 0"""
    boll = sym[tf]["bolling"]
    return (
        boll["Middle Band"][-1] > boll["Middle Band"][-2]
        and sym[tf]["macd"]["MACD_Line"][-1] > 0
    )


def is_1h_trend_up(sym: dict, tf: str) -> bool:
    """1 小时多头: 中轨不明显下行 + MACD 金叉发散"""
    boll = sym[tf]["bolling"]
    if boll["Middle Band"][-1] <= boll["Middle Band"][-2] * 0.999:
        return False
    macd = sym[tf]["macd"]
    return (
        macd["MACD_Line"][-1] >= macd["Signal_Line"][-1]
        and macd["MACD_Line"][-1] >= macd["MACD_Line"][-2]
        and macd["Signal_Line"][-1] >= macd["Signal_Line"][-2]
    )


def is_4h_trend_up(sym: dict, tf: str) -> bool:
    """4 小时多头: 中轨不明显下行 + DIF 上行"""
    boll = sym[tf]["bolling"]
    if boll["Middle Band"][-1] <= boll["Middle Band"][-2] * 0.999:
        return False
    return sym[tf]["macd"]["MACD_Line"][-1] >= sym[tf]["macd"]["MACD_Line"][-2]


def is_1d_trend_up(sym: dict) -> bool:
    """日线多头: 中轨连续上行 + 上轨连续上行 + 收盘价 > 中轨"""
    boll = sym["1D"]["bolling"]
    mid = boll["Middle Band"]
    upper = boll["Upper Band"]
    close = float(sym["1D"]["data"][-1][4])
    return (
        mid[-1] > mid[-2]
        and upper[-1] > upper[-2]
        and close > mid[-1]
    )

