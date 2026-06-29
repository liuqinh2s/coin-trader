"""
仓位管理模块：止盈、时间退出、价格追踪
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from infra.config import get_config
from infra.logger import log, notify
from infra.util import get_time_ms

if TYPE_CHECKING:
    from models import AccountState


def cut_profit(symbol: str, sym_data: dict, state: AccountState, order_fn) -> bool:
    """
    多仓出场逻辑：
    - 持仓超过 N 天仍未盈利则平仓
    - 持仓超过 N 天最高涨幅不达标则平仓
    - 浮盈达到启动门槛后的回撤止盈
    """
    cfg = get_config()
    data = sym_data["15m"]["data"]
    price = float(data[-1][4])
    price_avg = float(state.position[symbol]["openPriceAvg"])
    price_high = float(state.price_track[symbol]["priceHigh"])

    if state.position[symbol]["holdSide"] != "long":
        return False

    c_time = int(state.position[symbol]["cTime"])
    hold_days = (int(get_time_ms()) - c_time) / 1000 / 60 / 60 / 24
    max_gain_pct = (price_high - price_avg) / price_avg if price_avg > 0 else 0

    unprofitable_exit_days = cfg.get("unprofitable_exit_days", 2)
    if hold_days >= unprofitable_exit_days and price <= price_avg:
        pnl_pct = (price - price_avg) * 100 / price_avg if price_avg > 0 else 0
        reason = f"持仓超过{unprofitable_exit_days:g}天未盈利(当前{pnl_pct:.2f}%)"
        order_fn(symbol, data, "SELL", state, only_close=True, close_reason=reason)
        notify(f"{symbol} {reason}")
        return True

    weak_gain_exit_days = cfg.get("weak_gain_exit_days", 3)
    weak_gain_threshold_pct = cfg.get("weak_gain_threshold_pct", 0.06)
    if hold_days >= weak_gain_exit_days and max_gain_pct <= weak_gain_threshold_pct:
        reason = (
            f"持仓超过{weak_gain_exit_days:g}天最高涨幅不超过"
            f"{weak_gain_threshold_pct * 100:.0f}%"
        )
        order_fn(symbol, data, "SELL", state, only_close=True, close_reason=reason)
        notify(f"{symbol} {reason}，最高涨幅{max_gain_pct * 100:.2f}%")
        return True

    activation_pct = cfg.get("trailing_activation_pct", 0.06)
    pullback_pct = cfg.get("trailing_pullback_pct", 0.03)
    if (
        price_high > price_avg * (1 + activation_pct)
        and price_high > price * (1 + pullback_pct)
    ):
        high_pct = (price_high - price_avg) * 100 / price_avg
        reason = f"回撤止盈(最高涨{high_pct:.2f}%,回撤{pullback_pct * 100:.0f}%)"
        order_fn(symbol, data, "SELL", state, only_close=True, close_reason=reason)
        notify(f"止盈单，最高涨{high_pct:.2f}%，从高点回撤{pullback_pct * 100:.0f}%")
        return True

    return False


def track_price(all_sym: dict, is_first_scan: bool, state: AccountState) -> None:
    """追踪持仓期间的最高价和最低价，用于出场判断"""
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
                high_1m, state.price_track[sym]["priceHigh"]
            )
            state.price_track[sym]["priceLow"] = min(
                low_1m, state.price_track[sym]["priceLow"]
            )
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
