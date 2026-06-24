"""
市场扫描模块：成交量异动检测、辅助选币分析
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from infra.logger import log

if TYPE_CHECKING:
    from models import AccountState


# =============================================================================
#  成交量异动检测
# =============================================================================

def _is_15m_step_up(all_sym: dict, symbol: str, j: int) -> bool:
    """15 分钟布林中轨是否阶梯式上行"""
    mid = all_sym[symbol]["15m"]["bolling"]["Middle Band"]
    for i in range(-2, 0):
        diff_cur = mid[i + j] - mid[i - 1 + j]
        diff_prev = mid[i - 1 + j] - mid[i - 2 + j]
        if diff_cur < diff_prev * 0.9999:
            return False
        if diff_prev * 0.9999 < 0:
            return False
    return True


def _is_15m_anomaly(all_sym: dict, symbol: str, j: int, direction: str) -> bool:
    """15 分钟成交量异动检测"""
    sym = all_sym[symbol]
    data = sym["15m"]["data"]

    if direction == "buy":
        upper = sym["15m"]["bolling"]["Upper Band"][-3 + j]
        lower = sym["15m"]["bolling"]["Lower Band"][-3 + j]
        if upper > lower * 1.1 and not _is_15m_step_up(all_sym, symbol, j):
            return False
        upper_1h = sym["1H"]["bolling"]["Upper Band"][-3 + j]
        lower_1h = sym["1H"]["bolling"]["Lower Band"][-3 + j]
        if upper_1h > lower_1h * 1.22:
            return False

    vol_sum_9 = sum(float(data[i + j][6]) for i in range(-11, -2))
    vol_sum_19 = vol_sum_9 + sum(float(data[i + j][6]) for i in range(-21, -11))

    bar_vol = float(data[-2 + j][6])
    bar_close = float(data[-2 + j][4])
    bar_open = float(data[-2 + j][1])

    vol_short = bar_vol >= vol_sum_9 and bar_vol >= 100_000
    vol_long = bar_vol >= vol_sum_19 and bar_vol >= 40_000

    if direction == "buy":
        price_ok = bar_open * 0.992 < bar_close < bar_open * 1.23
    else:
        price_ok = bar_open * 0.945 < bar_close < bar_open * 1.008

    return (vol_short or vol_long) and price_ok


def _is_1h_anomaly(all_sym: dict, symbol: str, j: int, direction: str) -> bool:
    """1 小时成交量异动检测"""
    data = all_sym[symbol]["1H"]["data"]
    bar_vol = float(data[-1 + j][6])
    if bar_vol < 400_000:
        return False
    vol_sum = sum(float(data[i + j][6]) for i in range(-6, -1))
    if bar_vol < vol_sum:
        return False

    bar_close = float(data[-1 + j][4])
    bar_open = float(data[-1 + j][1])
    if direction == "buy":
        return bar_open * 1.02 < bar_close < bar_open * 1.5
    return bar_open * 0.93 < bar_close < bar_open * 0.98


def _is_4h_anomaly(all_sym: dict, symbol: str, j: int, direction: str) -> bool:
    """4 小时成交量异动检测"""
    data = all_sym[symbol]["4H"]["data"]
    bar_vol = float(data[-1 + j][6])
    if bar_vol < 800_000:
        return False
    vol_sum = sum(float(data[i + j][6]) for i in range(-5, -1))
    if bar_vol < vol_sum:
        return False

    bar_close = float(data[-1 + j][4])
    bar_open = float(data[-1 + j][1])
    if direction == "buy":
        return bar_open * 1.05 < bar_close < bar_open * 2
    return bar_open * 0.91 < bar_close < bar_open * 0.96


def _has_recent_anomaly_of(check_fn, all_sym, symbol, direction, lookback):
    """检查最近 lookback 根 K 线内是否已出现过异动"""
    return any(check_fn(all_sym, symbol, i, direction) for i in range(-lookback, 0))


def _has_any_recent_anomaly(all_sym: dict, symbol: str, direction: str) -> bool:
    """近期是否已出现过任意周期的异动"""
    return (
        _has_recent_anomaly_of(_is_15m_anomaly, all_sym, symbol, direction, 7)
        or _has_recent_anomaly_of(_is_1h_anomaly, all_sym, symbol, direction, 7)
        or _has_recent_anomaly_of(_is_4h_anomaly, all_sym, symbol, direction, 5)
    )


def detect_volume_anomaly(all_sym: dict, symbol: str, direction: str,
                          anomaly_dict: dict) -> str:
    """
    检测当前 K 线是否有成交量异动
    :return: 异动周期 ('15m' / '1H' / '') 并记录到 anomaly_dict
    """
    if _has_any_recent_anomaly(all_sym, symbol, direction):
        return ""
    if _is_15m_anomaly(all_sym, symbol, 0, direction):
        anomaly_dict["15m"].append(symbol)
        return "15m"
    if _is_1h_anomaly(all_sym, symbol, 0, direction):
        anomaly_dict["1H"].append(symbol)
        return "1H"
    return ""


def batch_detect_volume_anomaly(all_sym: dict, symbols: list[str],
                                direction: str) -> list[str]:
    """批量检测成交量异动，返回异动币种列表"""
    result: dict[str, list] = {"15m": [], "1H": [], "4H": []}
    for sym in symbols:
        if _has_any_recent_anomaly(all_sym, sym, direction):
            continue
        if _is_15m_anomaly(all_sym, sym, 0, direction):
            result["15m"].append(sym)
        if _is_1h_anomaly(all_sym, sym, 0, direction):
            result["1H"].append(sym)
        if _is_4h_anomaly(all_sym, sym, 0, direction):
            result["4H"].append(sym)

    log.info(
        "15m成交量异动：%s 1H成交量异动：%s 4H成交量异动：%s",
        result['15m'], result['1H'], result['4H'],
    )
    return result["15m"] + result["1H"] + result["4H"]


# =============================================================================
#  辅助分析
# =============================================================================

def select_by_fund_rate(state: AccountState) -> None:
    """筛选资金费率有利的币种"""
    from api.factory import get_exchange

    ex = get_exchange()
    for label, side_list, threshold, cmp in [
        ("上涨趋势+资金费为负", state.buy_list, -0.05, lambda t, th: t < th),
    ]:
        if not side_list:
            continue
        result = []
        for sym in side_list:
            fund_rate = ex.get_history_fund_rate(sym, ex.PRODUCT_TYPE)
            total = sum(float(x["fundingRate"]) for x in fund_rate["data"])
            if cmp(total, threshold):
                result.append(sym)
        log.info("%s：%s", label, result)


def select_by_volume(all_sym: dict, state: AccountState) -> list[str]:
    """筛选小成交量 + 不错涨跌幅的币种"""
    if not state.buy_list:
        return []
    result = [
        sym for sym in state.buy_list
        if (float(all_sym[sym]["1D"]["data"][-1][2]) > float(all_sym[sym]["1D"]["data"][-1][1]) * 1.2
            and float(all_sym[sym]["1D"]["data"][-1][6]) < 6_000_000)
    ]
    log.info("小成交量+不错的涨幅：%s", result)
    return result


def select_by_volume_surge(all_sym: dict, state: AccountState) -> None:
    """筛选日成交量比前三日之和还多的币种"""
    if not state.buy_list:
        return
    result = []
    for sym in state.buy_list:
        vol_sum = sum(float(all_sym[sym]["1D"]["data"][i][6]) for i in range(-4, -1))
        bar = all_sym[sym]["1D"]["data"][-1]
        cur_vol = float(bar[6])
        cur_change = float(bar[4]) / float(bar[1])
        if cur_vol > vol_sum and (cur_vol > 10_000_000 or cur_change > 1.2):
            result.append(sym)
    log.info("日成交量比前三日加起来还多：%s", result)


def find_leading_coins(all_sym: dict) -> list[str]:
    """找出近 5 天内有 20% 以上涨幅的龙头币"""
    result = []
    for key in all_sym:
        data = all_sym[key]["1D"]["data"]
        if len(data) < 20:
            continue
        for i in range(-5, -1):
            if float(data[-1 + i][4]) > float(data[-5 + i][4]) * 1.2:
                result.append(key)
                break
    log.info("近5天的龙头币: %s", result)
    return result


def find_fairy_guide(all_sym: dict, state: AccountState) -> list[str]:
    """
    仙人指路形态：近 10 日内有一根日 K 满足：
    成交量 > 前 9 日之和，最高价涨幅 20%~60%，回落 > 8%，收阳线
    """
    result = []
    if not state.buy_list:
        return result
    for sym in state.buy_list:
        data = all_sym[sym]["1D"]["data"]
        if len(data) < 10:
            continue
        for i in range(-10, 0):
            vol_sum = sum(float(data[i + j][6]) for j in range(-10, -1))
            bar = data[i]
            o, h, c, v = float(bar[1]), float(bar[2]), float(bar[4]), float(bar[6])
            if v > vol_sum and o * 1.2 < h < o * 1.6 and h * 0.92 > c > o:
                result.append(sym)
                break
    log.info("仙人指路：%s", result)
    return result


def detect_bottom_volume_surge(sym: dict) -> bool:
    """
    底部放量：
    最近 3 日成交额均大于 100 万 U，且均大于第一次放量日前 20 日
    平均成交额的 5 倍；当前价格比第一次放量前一日高 30% 以上。
    """
    try:
        data = sym.get("1D", {}).get("data") or []
        if len(data) < 24:
            return False

        first_surge_idx = -3
        baseline_bars = data[first_surge_idx - 20:first_surge_idx]
        surge_bars = data[first_surge_idx:]
        if len(baseline_bars) != 20 or len(surge_bars) != 3:
            return False

        baseline_avg = sum(float(bar[6]) for bar in baseline_bars) / 20
        if baseline_avg <= 0:
            return False

        if any(float(bar[6]) <= 1_000_000 for bar in surge_bars):
            return False
        if any(float(bar[6]) <= baseline_avg * 5 for bar in surge_bars):
            return False

        price_before_surge = float(data[first_surge_idx - 1][4])
        current_price = float(data[-1][4])
        return price_before_surge > 0 and current_price > price_before_surge * 1.3
    except (IndexError, KeyError, ValueError, TypeError):
        return False


# =============================================================================
#  盘整放量突破检测（源自 temp.py 策略）
# =============================================================================

def detect_consolidation_breakout(sym: dict, cycle: str = "1H") -> bool:
    """
    检测盘整初期放量突破：均线收敛 → 放量 → 突破新高

    基于 1H 周期数据，需要以下指标已计算：
    ma30/ma60/ma120/ma160/ma200、rsi、volume_osc、data

    条件：
    1. 涨幅适中（1%~6%），收盘站上所有均线，实体 > 上影线
    2. 成交量放量（volume_osc > 40%，或 > 15% 且均线多头排列）
    3. 收盘创近 120 根 K 线新高，且高于前 3 根最高价
    4. 均线收敛（最大最小均线差 < 2.8%）
    5. RSI 在 58~80
    6. 回溯 10 根 K 线，每根均线宽度 < 3.5% 且 RSI 在 38~82
    """
    try:
        c = sym.get(cycle)
        if not c:
            return False

        data = c.get("data", [])
        rsi = c.get("rsi", [])
        vol_osc = c.get("volume_osc", [])

        # 需要足够的数据
        if len(data) < 200 or len(rsi) < 12 or len(vol_osc) < 2:
            return False

        ma_keys = ["ma30", "ma60", "ma120", "ma160", "ma200"]
        for k in ma_keys:
            if k not in c or len(c[k]) < 12:
                return False

        # 当前 K 线
        close = float(data[-1][4])
        open_ = float(data[-1][1])
        high = float(data[-1][2])

        # 条件 1：涨幅 1%~6%，站上所有均线，实体 > 上影线
        zf = (close - open_) / open_ if open_ > 0 else 0
        ma_values = [c[k][-1] for k in ma_keys]
        ma_max = max(v for v in ma_values if v is not None and v == v)  # 排除 NaN
        ma_min = min(v for v in ma_values if v is not None and v == v)
        if not (0.01 < zf < 0.06):
            return False
        if close <= ma_max:
            return False
        if (close - open_) <= (high - close):
            return False

        # 条件 2：放量
        cur_vol_osc = vol_osc[-1]
        ma30_val = c["ma30"][-1]
        ma60_val = c["ma60"][-1]
        ma120_val = c["ma120"][-1]
        ma200_val = c["ma200"][-1]
        bullish_aligned = (ma30_val and ma60_val and ma120_val and ma200_val
                           and ma30_val > ma60_val > ma120_val > ma200_val)
        if not (cur_vol_osc > 40 or (cur_vol_osc > 15 and bullish_aligned)):
            return False

        # 条件 3：创 120 根 K 线新高
        recent_highs = [float(data[i][2]) for i in range(-4, -1)]
        closes_120 = [float(data[i][4]) for i in range(-121, -1)]
        if not closes_120:
            return False
        max_close_120 = max(closes_120)
        if close <= max_close_120:
            return False
        if not all(close > h for h in recent_highs):
            return False

        # 条件 4：均线收敛 < 2.8%
        if ma_min <= 0:
            return False
        if (ma_max - ma_min) / ma_min >= 0.028:
            return False

        # 条件 5：RSI 58~80
        cur_rsi = rsi[-1]
        if not (58 < cur_rsi < 80):
            return False

        # 条件 6：回溯 10 根 K 线，均线宽度 < 3.5% 且 RSI 38~82
        for i in range(2, 11):
            i_ma_values = [c[k][-i] for k in ma_keys]
            i_valid = [v for v in i_ma_values if v is not None and v == v]
            if len(i_valid) < 5:
                return False
            i_max = max(i_valid)
            i_min = min(i_valid)
            if i_min <= 0 or (i_max - i_min) / i_max >= 0.035:
                return False
            if len(rsi) >= i and not (38 < rsi[-i] < 82):
                return False

        return True

    except (KeyError, IndexError, ValueError, TypeError):
        return False


# =============================================================================
#  强势启动检测（4H MA 多头排列加速）
# =============================================================================

def detect_early_strong_trend(sym: dict) -> bool:
    """
    强势启动：4H 周期 MA 多头排列且加速发散

    条件：
    - ma10 > ma20 > ma40
    - ma80 > ma160
    - ma10 > ma80
    - (ma10 - ma20) > (前7根ma10 - 前7根ma20) * 2
    - (ma20 - ma40) > (前7根ma20 - 前7根ma40) * 2
    - (前7根ma20 - 前7根ma40) / 前7根ma40 in [1.01, 1.015)
    """
    try:
        c = sym.get("4H")
        if not c:
            return False

        ma_keys = ["ma10", "ma20", "ma40", "ma80", "ma160"]
        for k in ma_keys:
            if k not in c or len(c[k]) < 9:
                return False

        ma10 = c["ma10"]
        ma20 = c["ma20"]
        ma40 = c["ma40"]
        ma80 = c["ma80"]
        ma160 = c["ma160"]

        if not (ma10[-1] > ma20[-1] > ma40[-1]):
            return False

        if not (ma80[-1] > ma160[-1]):
            return False

        if not (ma10[-1] > ma80[-1]):
            return False

        cur_diff_10_20 = ma10[-1] - ma20[-1]
        prev_diff_10_20 = ma10[-8] - ma20[-8]
        if cur_diff_10_20 <= prev_diff_10_20 * 2:
            return False

        cur_diff_20_40 = ma20[-1] - ma40[-1]
        prev_diff_20_40 = ma20[-8] - ma40[-8]
        if cur_diff_20_40 <= prev_diff_20_40 * 2:
            return False

        if ma40[-8] <= 0:
            return False
        prev_ratio = prev_diff_20_40 / ma40[-8]
        if prev_ratio < 1.01:
            return False
        if prev_ratio >= 1.015:
            return False

        return True
    except (KeyError, IndexError, ValueError, TypeError):
        return False
