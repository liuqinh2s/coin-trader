"""
仓位管理模块：止盈止损、价格追踪
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from infra.config import get_config
from infra.logger import log, notify
from infra.util import get_time_ms

if TYPE_CHECKING:
    from models import AccountState

# 时间常量（毫秒）
MS_1D = 24 * 60 * 60 * 1000


def _ms_to_days(ms: int | float) -> float:
    """毫秒 → 天"""
    return ms / 1000 / 60 / 60 / 24


def _to_decimal(value) -> Decimal:
    return Decimal(str(value))


def _calc_partial_take_profit(
    price: Decimal,
    price_avg: Decimal,
    available: Decimal,
    track: dict,
    cfg: dict,
) -> tuple[int, Decimal, Decimal, Decimal] | None:
    """计算需要补执行的分段止盈段数和卖出数量。"""
    step_pct = _to_decimal(cfg.get("partial_take_profit_step_pct", 0.02))
    sell_pct = _to_decimal(cfg.get("partial_take_profit_sell_pct", 0.02))
    if step_pct <= 0 or sell_pct <= 0 or available <= 0:
        return None

    current_stage = int(track.get("partialTakeProfitCount", 0))
    target_stage = current_stage
    next_trigger = price_avg * ((Decimal("1") + step_pct) ** (target_stage + 1))
    while price >= next_trigger:
        target_stage += 1
        next_trigger = price_avg * ((Decimal("1") + step_pct) ** (target_stage + 1))

    crossed = target_stage - current_stage
    if crossed <= 0:
        return None

    sell_ratio = Decimal("1") - ((Decimal("1") - sell_pct) ** crossed)
    close_size = available * sell_ratio
    trigger_price = price_avg * ((Decimal("1") + step_pct) ** target_stage)
    return target_stage, close_size, sell_ratio, trigger_price


def cut_profit(symbol: str, sym_data: dict, state: AccountState,
               order_fn) -> bool:
    """
    动态止盈逻辑（仅多仓）：
    - 持仓超时止损 / 布林上轨下弯 / 阶梯回撤止盈 / 分段止盈
    :param order_fn: order 函数引用（避免循环导入）
    :return: True 表示已平仓
    """
    cfg = get_config()
    data = sym_data["15m"]["data"]
    price = float(data[-1][4])
    price_dec = _to_decimal(data[-1][4])
    price_avg = float(state.position[symbol]["openPriceAvg"])
    price_avg_dec = _to_decimal(state.position[symbol]["openPriceAvg"])
    price_high = float(state.price_track[symbol]["priceHigh"])
    c_time = int(state.position[symbol]["cTime"])
    hold_ms = int(data[-1][0]) - c_time

    if state.position[symbol]["holdSide"] != "long":
        return False

    # 持仓超 N 天未盈利（默认1天=24小时）
    timeout_loss = cfg.get("long_timeout_loss_days", 1)
    if price <= price_avg and hold_ms > MS_1D * timeout_loss:
        reason = f"超时未盈利({timeout_loss * 24:.0f}h)"
        order_fn(symbol, data, "SELL", state, only_close=True, close_reason=reason)
        notify(f"持仓超过{timeout_loss * 24:.0f}小时，未盈利，平仓")
        return True

    # 持仓超 N 天盈利未达止盈最低触发标准（默认2天=48小时）
    timeout_profit = cfg.get("long_timeout_profit_days", 2)
    min_profit_pct = cfg.get("long_min_profit_pct", 0.06)
    if price < price_avg * (1 + min_profit_pct) and hold_ms > MS_1D * timeout_profit:
        cur_pct = (price - price_avg) / price_avg * 100
        reason = f"超时盈利不足({timeout_profit * 24:.0f}h, 当前{cur_pct:.1f}%<{min_profit_pct*100:.0f}%)"
        order_fn(symbol, data, "SELL", state, only_close=True, close_reason=reason)
        notify(f"持仓超过{timeout_profit * 24:.0f}小时，盈利未达{min_profit_pct*100:.0f}%，平仓")
        return True

    # 布林上轨连续两日下弯（需要至少 3 个有效值）
    upper = [v for v in sym_data["1D"]["bolling"]["Upper Band"] if v == v]  # 过滤 NaN
    if len(upper) >= 3 and upper[-1] < upper[-2] < upper[-3]:
        order_fn(symbol, data, "SELL", state, only_close=True, close_reason="布林上轨连续下弯")
        notify("布林线上轨下弯，平仓")
        return True

    # 回撤止盈：浮盈达到启动门槛(默认6%)后开始跟踪，从最高点回撤固定比例(默认3%)即止盈
    activation_pct = cfg.get("trailing_activation_pct", 0.06)
    pullback_pct = cfg.get("trailing_pullback_pct", 0.03)
    if price_high > price_avg * (1 + activation_pct) and price_high > price * (1 + pullback_pct):
        high_pct = (price_high - price_avg) * 100 / price_avg
        reason = f"回撤止盈(最高涨{high_pct:.2f}%,回撤{pullback_pct * 100:.0f}%)"
        order_fn(symbol, data, "SELL", state, only_close=True, close_reason=reason)
        notify(f"止盈单，最高涨{high_pct:.2f}%，从高点回撤{pullback_pct * 100:.0f}%")
        return True

    # 分段止盈：每上涨 2% 卖出剩余持仓的 2%，涨幅按上一段触发价复利计算。
    available = _to_decimal(state.position[symbol]["available"])
    partial = _calc_partial_take_profit(
        price_dec, price_avg_dec, available, state.price_track[symbol], cfg,
    )
    if partial:
        target_stage, close_size, sell_ratio, trigger_price = partial
        stage_before = int(state.price_track[symbol].get("partialTakeProfitCount", 0))
        trigger_pct = (trigger_price - price_avg_dec) / price_avg_dec * 100
        current_pct = (price_dec - price_avg_dec) / price_avg_dec * 100
        if target_stage == stage_before + 1:
            stage_desc = f"第{target_stage}段"
        else:
            stage_desc = f"第{stage_before + 1}-{target_stage}段"
        reason = (
            f"分段止盈({stage_desc},涨至{trigger_pct:.2f}%,"
            f"卖剩余{sell_ratio * 100:.2f}%)"
        )
        result = order_fn(
            symbol, data, "SELL", state, only_close=True,
            close_reason=reason, close_size=close_size,
        )
        if result is not None and symbol in state.price_track:
            state.price_track[symbol]["partialTakeProfitCount"] = target_stage
            state.price_track[symbol]["partialTakeProfitPrice"] = float(trigger_price)
            notify(
                f"分段止盈，{symbol} {stage_desc}，"
                f"当前涨{current_pct:.2f}%，卖出剩余持仓{sell_ratio * 100:.2f}%"
            )

    return False


def track_price(all_sym: dict, is_first_scan: bool, state: AccountState) -> None:
    """追踪持仓期间的最高价和最低价，用于止盈判断"""
    # 清理已平仓的记录
    for sym in list(state.price_track.keys()):
        if sym not in all_sym:
            del state.price_track[sym]

    for sym in all_sym:
        if is_first_scan:
            c_time = int(state.position[sym]["cTime"])
            high_max = float("-inf")
            low_min = float("inf")
            price_start = None
            for bar in all_sym[sym]["15m"]["data"]:
                if int(bar[0]) < c_time:
                    # 记录开仓前最后一根 bar 的收盘价作为 priceStart
                    price_start = float(bar[3])
                    continue
                high_max = max(high_max, float(bar[2]))
                low_min = min(low_min, float(bar[3]))
            if high_max > float("-inf"):
                if price_start is None:
                    price_start = float(all_sym[sym]["15m"]["data"][0][3])
                state.price_track[sym] = {
                    "priceHigh": high_max,
                    "priceLow": low_min,
                    "priceStart": price_start,
                }

        bar_1m = all_sym[sym]["1m"]["data"][-1]
        high_1m, low_1m = float(bar_1m[2]), float(bar_1m[3])
        if sym in state.price_track:
            state.price_track[sym]["priceHigh"] = max(
                high_1m, state.price_track[sym]["priceHigh"])
            state.price_track[sym]["priceLow"] = min(
                low_1m, state.price_track[sym]["priceLow"])
        else:
            state.price_track[sym] = {
                "priceHigh": high_1m,
                "priceLow": low_1m,
                "priceStart": float(all_sym[sym]["15m"]["data"][-2][3]),
            }

        open_price = float(state.position[sym]["openPriceAvg"])
        state.price_track[sym]["rate"] = (
            state.price_track[sym]["priceHigh"] - open_price
        ) / open_price
