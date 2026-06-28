"""
仓位管理模块：止盈止损、价格追踪
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from infra.config import get_config
from infra.logger import log, notify

if TYPE_CHECKING:
    from models import AccountState

def cut_profit(symbol: str, sym_data: dict, state: AccountState,
               order_fn) -> bool:
    """
    动态止盈逻辑（仅多仓）：
    - 回撤止盈
    :param order_fn: order 函数引用（避免循环导入）
    :return: True 表示已平仓
    """
    cfg = get_config()
    data = sym_data["15m"]["data"]
    price = float(data[-1][4])
    price_avg = float(state.position[symbol]["openPriceAvg"])
    price_high = float(state.price_track[symbol]["priceHigh"])

    if state.position[symbol]["holdSide"] != "long":
        return False

    # 回撤止盈：浮盈达到启动门槛(默认6%)后开始跟踪，从最高点回撤固定比例(默认3%)即止盈
    activation_pct = cfg.get("trailing_activation_pct", 0.06)
    pullback_pct = cfg.get("trailing_pullback_pct", 0.03)
    if price_high > price_avg * (1 + activation_pct) and price_high > price * (1 + pullback_pct):
        high_pct = (price_high - price_avg) * 100 / price_avg
        reason = f"回撤止盈(最高涨{high_pct:.2f}%,回撤{pullback_pct * 100:.0f}%)"
        order_fn(symbol, data, "SELL", state, only_close=True, close_reason=reason)
        notify(f"止盈单，最高涨{high_pct:.2f}%，从高点回撤{pullback_pct * 100:.0f}%")
        return True

    return False


def track_price(all_sym: dict, is_first_scan: bool, state: AccountState) -> None:
    """追踪持仓期间的最高价和最低价，用于止盈判断"""
    # 清理已平仓的记录
    for sym in list(state.price_track.keys()):
        if sym not in all_sym:
            del state.price_track[sym]

    for sym in all_sym:
        if sym not in state.position:
            continue

        data_15m = all_sym[sym].get("15m", {}).get("data") or []
        data_1m = all_sym[sym].get("1m", {}).get("data") or []
        if not data_1m:
            log.warning("持仓价格追踪跳过 %s：1m K线为空", sym)
            continue
        if not data_15m:
            log.warning("持仓价格追踪跳过 %s：15m K线为空", sym)
            continue

        if is_first_scan:
            c_time = int(state.position[sym]["cTime"])
            high_max = float("-inf")
            low_min = float("inf")
            price_start = None
            for bar in data_15m:
                if int(bar[0]) < c_time:
                    # 记录开仓前最后一根 bar 的收盘价作为 priceStart
                    price_start = float(bar[3])
                    continue
                high_max = max(high_max, float(bar[2]))
                low_min = min(low_min, float(bar[3]))
            if high_max > float("-inf"):
                if price_start is None:
                    price_start = float(data_15m[0][3])
                state.price_track[sym] = {
                    "priceHigh": high_max,
                    "priceLow": low_min,
                    "priceStart": price_start,
                }

        bar_1m = data_1m[-1]
        high_1m, low_1m = float(bar_1m[2]), float(bar_1m[3])
        if sym in state.price_track:
            state.price_track[sym]["priceHigh"] = max(
                high_1m, state.price_track[sym]["priceHigh"])
            state.price_track[sym]["priceLow"] = min(
                low_1m, state.price_track[sym]["priceLow"])
        else:
            price_start = data_15m[-2][3] if len(data_15m) >= 2 else data_15m[-1][3]
            state.price_track[sym] = {
                "priceHigh": high_1m,
                "priceLow": low_1m,
                "priceStart": float(price_start),
            }

        open_price = float(state.position[sym]["openPriceAvg"])
        state.price_track[sym]["rate"] = (
            state.price_track[sym]["priceHigh"] - open_price
        ) / open_price
