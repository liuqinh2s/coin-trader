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

    baseline_volumes = [float(data[i + j][6]) for i in range(-11, -2)]
    vol_sum_8 = sum(baseline_volumes) - max(baseline_volumes)

    bar_vol = float(data[-2 + j][6])
    bar_close = float(data[-2 + j][4])
    bar_open = float(data[-2 + j][1])

    vol_short = bar_vol >= vol_sum_8 and bar_vol >= 100_000

    if direction == "buy":
        price_ok = bar_open * 1.02 < bar_close < bar_open * 1.23
    else:
        price_ok = bar_open * 0.945 < bar_close < bar_open * 1.008

    return vol_short and price_ok


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
        if len(data) < 19:
            continue
        start_idx = max(len(data) - 10, 9)
        for i in range(start_idx, len(data)):
            vol_sum = sum(float(bar[6]) for bar in data[i - 9:i])
            bar = data[i]
            o, h, c, v = float(bar[1]), float(bar[2]), float(bar[4]), float(bar[6])
            if v > vol_sum and o * 1.2 < h < o * 1.6 and h * 0.92 > c > o:
                result.append(sym)
                break
    log.info("仙人指路：%s", result)
    return result


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
