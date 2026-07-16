"""
仓位管理模块：止盈、时间退出、价格追踪。
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

from infra.config import get_config
from infra.logger import log, notify

if TYPE_CHECKING:
    from models import AccountState


def _get_trailing_stop_tier(
    price_avg: float,
    price_high: float,
    tiers: list[list[float]],
    gain_step: float,
    pullback_step: float,
) -> tuple[float, float] | None:
    """返回最高价已达到的涨幅档位，以及按买入价计算的回撤比例。"""
    if price_avg <= 0 or not tiers:
        return None

    sorted_tiers = sorted(
        ((float(gain), float(pullback)) for gain, pullback in tiers),
        key=lambda tier: tier[0],
    )
    gain = price_high / price_avg - 1
    epsilon = 1e-12

    if gain + epsilon < sorted_tiers[0][0]:
        return None

    selected_gain, selected_pullback = sorted_tiers[0]
    for tier_gain, tier_pullback in sorted_tiers[1:]:
        if gain + epsilon < tier_gain:
            break
        selected_gain, selected_pullback = tier_gain, tier_pullback

    last_gain, last_pullback = sorted_tiers[-1]
    if selected_gain == last_gain and gain_step > 0 and pullback_step > 0:
        extra_steps = math.floor((gain - last_gain + epsilon) / gain_step)
        selected_gain = round(last_gain + extra_steps * gain_step, 12)
        selected_pullback = round(last_pullback + extra_steps * pullback_step, 12)

    return selected_gain, selected_pullback


def cut_profit(symbol: str, sym_data: dict, state: AccountState, order_fn) -> bool:
    """
    多仓出场逻辑：
    - 浮盈达到启动门槛后执行阶梯回撤止盈
    - 回撤金额始终以买入均价为基准计算
    """
    cfg = get_config()
    data = sym_data["15m"]["data"]
    price = float(data[-1][4])
    price_avg = float(state.position[symbol]["openPriceAvg"])
    price_high = float(state.price_track[symbol]["priceHigh"])

    if state.position[symbol]["holdSide"] != "long":
        return False

    tiers = cfg.get("trailing_stop_tiers")
    if not tiers:
        log.error("config.yaml 缺少 trailing_stop_tiers 配置，跳过止盈检查")
        return False

    tier = _get_trailing_stop_tier(
        price_avg,
        price_high,
        tiers,
        float(cfg.get("trailing_stop_gain_step", 0.05)),
        float(cfg.get("trailing_stop_pullback_step", 0.01)),
    )
    if tier is None:
        return False

    tier_gain, pullback = tier
    trigger_price = price_high - price_avg * pullback
    if price <= trigger_price:
        reason = (
            f"阶梯止盈(涨{tier_gain * 100:.0f}%,"
            f"按买入价回撤{pullback * 100:.0f}%)"
        )
        order_fn(symbol, data, "SELL", state, only_close=True, close_reason=reason)
        notify(
            f"止盈单，涨{tier_gain * 100:.0f}%，"
            f"按买入价回撤{pullback * 100:.0f}%"
        )
        return True

    return False


def track_price(all_sym: dict, is_first_scan: bool, state: AccountState) -> None:
    """追踪持仓期间的最高价和最低价，用于出场判断。"""
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
