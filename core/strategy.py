"""
策略模块：趋势判断（多周期）、BTC 大盘方向过滤
"""
from __future__ import annotations


# =============================================================================
#  通用趋势判断
# =============================================================================

def is_trend_up(symbol_data: dict, tf: str, offset: int = 0) -> bool:
    """短周期多头趋势: 布林中轨上行 + MACD 双线 > 0 且金叉发散"""
    boll = symbol_data[tf]["bolling"]
    macd = symbol_data[tf]["macd"]
    i = offset
    mid_rising = boll["Middle Band"][-1 + i] > boll["Middle Band"][-2 + i]
    macd_bull = (
        macd["MACD_Line"][-1 + i] > 0
        and macd["Signal_Line"][-1 + i] > 0
        and macd["MACD_Line"][-1 + i] > macd["Signal_Line"][-1 + i]
        and macd["MACD_Line"][-1 + i] > macd["MACD_Line"][-2 + i]
        and macd["Signal_Line"][-1 + i] > macd["Signal_Line"][-2 + i]
    )
    return mid_rising and macd_bull


def is_trend_down(symbol_data: dict, tf: str, offset: int = 0) -> bool:
    """短周期空头趋势: 布林中轨下行 + MACD 双线 < 0 且死叉发散"""
    boll = symbol_data[tf]["bolling"]
    macd = symbol_data[tf]["macd"]
    i = offset
    mid_falling = boll["Middle Band"][-1 + i] < boll["Middle Band"][-2 + i]
    macd_bear = (
        macd["MACD_Line"][-1 + i] < 0
        and macd["Signal_Line"][-1 + i] < 0
        and macd["MACD_Line"][-1 + i] < macd["Signal_Line"][-1 + i]
        and macd["MACD_Line"][-1 + i] < macd["MACD_Line"][-2 + i]
        and macd["Signal_Line"][-1 + i] < macd["Signal_Line"][-2 + i]
    )
    return mid_falling and macd_bear


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


def is_15m_trend_down(sym: dict, tf: str) -> bool:
    """15 分钟空头: 中轨下行 + DIF < 0"""
    boll = sym[tf]["bolling"]
    if boll["Middle Band"][-1] >= boll["Middle Band"][-2]:
        return False
    return sym[tf]["macd"]["MACD_Line"][-1] < 0


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


def is_1h_trend_down(sym: dict, tf: str) -> bool:
    """1 小时空头: 中轨不明显上行 + MACD 双线 <= 0"""
    boll = sym[tf]["bolling"]
    if boll["Middle Band"][-1] >= boll["Middle Band"][-2] * 0.999:
        return False
    macd = sym[tf]["macd"]
    return macd["MACD_Line"][-1] <= 0 and macd["Signal_Line"][-1] <= 0


def is_4h_trend_up(sym: dict, tf: str) -> bool:
    """4 小时多头: 中轨不明显下行 + DIF 上行"""
    boll = sym[tf]["bolling"]
    if boll["Middle Band"][-1] <= boll["Middle Band"][-2] * 0.999:
        return False
    return sym[tf]["macd"]["MACD_Line"][-1] >= sym[tf]["macd"]["MACD_Line"][-2]


def is_4h_trend_down(sym: dict, tf: str) -> bool:
    """4 小时空头: 中轨不明显上行 + MACD 空头条件"""
    boll = sym[tf]["bolling"]
    if boll["Middle Band"][-1] >= boll["Middle Band"][-2] * 0.999:
        return False
    macd = sym[tf]["macd"]
    bearish_zone = macd["MACD_Line"][-1] <= 0 and macd["Signal_Line"][-1] <= 0
    diverging_down = (
        macd["MACD_Line"][-1] <= macd["Signal_Line"][-1]
        and macd["MACD_Line"][-1] <= macd["MACD_Line"][-2]
        and macd["Signal_Line"][-1] <= macd["Signal_Line"][-2]
    )
    return bearish_zone or diverging_down


def is_1d_trend_up(sym: dict) -> bool:
    """日线多头: 中轨连续上行 + 上轨连续上行 + 收盘价 > 中轨"""
    boll = sym["1D"]["bolling"]
    mid = boll["Middle Band"]
    upper = boll["Upper Band"]
    close = float(sym["1D"]["data"][-1][4])
    return (
        mid[-1] > mid[-2] > mid[-3] > mid[-4]
        and upper[-1] > upper[-2] > upper[-3]
        and close > mid[-1]
    )


# =============================================================================
#  BTC 大盘方向
# =============================================================================

def is_btc_trend_down(all_sym: dict) -> bool:
    """BTC 是否处于短期下跌趋势（多条件 OR）"""
    btc = all_sym.get("BTCUSDT")
    if not btc:
        return True  # 拿不到 BTC 数据时保守处理，视为下跌
    btc_1d = btc["1D"]["data"]
    btc_1h = btc["1H"]["data"]
    if len(btc_1d) < 7 or len(btc_1h) < 25:
        return True
    close = float(btc_1d[-1][4])
    return any([
        float(btc_1d[-1][1]) > close * 1.02,
        float(btc_1h[-25][4]) > close * 1.02,
        float(btc_1d[-7][4]) > close * 1.05,
        float(btc_1d[-5][4]) > close * 1.05,
        float(btc_1d[-3][4]) > close * 1.03,
        float(btc_1d[-4][4]) > close * 1.04,
    ])


def is_btc_trend_up(all_sym: dict) -> bool:
    """BTC 日线收盘价比开盘价高 2% 以上"""
    btc = all_sym.get("BTCUSDT")
    if not btc or len(btc["1D"]["data"]) == 0:
        return False  # 拿不到 BTC 数据时保守处理，不开仓
    bar = btc["1D"]["data"][-1]
    return float(bar[4]) > float(bar[1]) * 1.02


def is_btc_12h_not_down(all_sym: dict) -> bool:
    """BTC 近 12 小时未下跌：当前收盘价 >= 12 小时前收盘价"""
    btc = all_sym.get("BTCUSDT")
    if not btc:
        return False
    btc_1h = btc.get("1H", {}).get("data") or []
    if len(btc_1h) < 12:
        return False
    cur_close = float(btc_1h[-1][4])
    close_12h_ago = float(btc_1h[-12][4])
    return cur_close >= close_12h_ago
