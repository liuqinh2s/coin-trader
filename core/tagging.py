"""共享的选币标签逻辑。

前端扫描器（scripts/scan.py）与实时自动交易（core/live_trading.py）共用这里的
标签判定，确保两边"标签集合"完全一致，避免漂移。
"""
from __future__ import annotations

from typing import Any

from core.auto_strategy import (
    evaluate_auto_trade_conditions,
)
from core.scanner import (
    detect_bottom_volume_surge,
    detect_consolidation_breakout,
    detect_early_strong_trend,
    detect_volume_anomaly,
)
from core.strategy import (
    is_15m_trend_up,
    is_1d_boll_trend_up,
    is_1d_trend_up,
    is_1h_trend_up,
    is_4h_trend_up,
)


# =============================================================================
#  单币标签判定（不依赖外部 API / 跨币数据）
# =============================================================================

def min_price_7d(sym: dict) -> float:
    data = sym["1D"]["data"]
    days = min(7, len(data))
    return min(float(data[-i][3]) for i in range(1, days + 1))


def check_anti_chase(sym: dict, cfg: dict[str, Any]) -> bool:
    """长线未追高：近 7 日涨幅、布林带宽、收盘价相对上轨均未过度拉升。"""
    try:
        close = float(sym["1D"]["data"][-1][4])
        boll = sym["1D"]["bolling"]
        return (
            close < min_price_7d(sym) * cfg.get("max_7d_gain_mult", 2.7)
            and boll["Upper Band"][-1] < boll["Lower Band"][-1] * cfg.get("max_boll_width_mult", 2.7)
            and close < boll["Upper Band"][-1] * cfg.get("max_close_above_upper_mult", 1.1)
        )
    except (IndexError, KeyError, ValueError):
        return False


def check_short_anti_chase(sym: dict) -> bool:
    """短线未追高：当日涨幅 <= 20%，且价格 <= 日K 的 MA7 * 1.2"""
    try:
        data = sym["1D"]["data"]
        if len(data) < 7:
            return False
        close = float(data[-1][4])
        open_price = float(data[-1][1])
        daily_gain = (close - open_price) / open_price if open_price > 0 else 0
        if daily_gain > 0.20:
            return False
        ma7 = sum(float(x[4]) for x in data[-7:]) / 7
        return ma7 > 0 and close <= ma7 * 1.2
    except (IndexError, KeyError, ValueError):
        return False


def check_ma60_up(sym: dict) -> bool:
    """MA60向上：日K 的 MA60 今日 > 昨日"""
    try:
        ma60 = sym["1D"]["ma60"]
        today, yesterday = ma60[-1], ma60[-2]
        # 排除 NaN（rolling 均线早期为 NaN）
        if today != today or yesterday != yesterday:
            return False
        return today > yesterday
    except (IndexError, KeyError, ValueError, TypeError):
        return False


def check_short_pullback(sym: dict) -> bool:
    """短期回调：当前 4H 收盘价相对前 6 根 4H 高点回落至少 10%。"""
    try:
        data = sym["4H"]["data"]
        if len(data) < 7:
            return False
        prev_high = max(float(b[2]) for b in data[-7:-1])
        current_price = float(data[-1][4])
        return prev_high > 0 and current_price <= prev_high * 0.90
    except (IndexError, KeyError, ValueError):
        return False


def is_not_rubbish(sym: dict) -> bool:
    """波动充足：近 3 日内任一日振幅 > 10%。"""
    try:
        for i in range(-3, 0):
            if float(sym["1D"]["data"][i][2]) > float(sym["1D"]["data"][i][3]) * 1.1:
                return True
    except (IndexError, KeyError, ValueError):
        return False
    return False


def is_trend_confluence(sym: dict) -> bool:
    return (
        is_15m_trend_up(sym, "15m")
        and is_1h_trend_up(sym, "1H")
        and is_4h_trend_up(sym, "4H")
        and is_1d_trend_up(sym)
    )


# =============================================================================
#  组装单币标签列表（与 scripts/scan.py 主循环逐条对应）
# =============================================================================

def build_symbol_tags(
    all_sym: dict,
    key: str,
    sym: dict,
    cfg: dict[str, Any],
    market_cap_info: dict[str, Any] | None,
    fund_rate: float,
    leading: set[str],
    anomaly_dict: dict,
) -> list[str]:
    """组装单个币的标签列表（不含需要候选集的 小量大涨/仙人指路）。

    与 scripts/scan.py 主循环的标签判定逐条一致。
    """
    auto_trade_cfg = cfg.get("auto_trade", {})
    tags: list[str] = []

    try:
        if is_trend_confluence(sym):
            tags.append("趋势共振")
    except (IndexError, KeyError, ValueError):
        pass

    try:
        if is_1d_boll_trend_up(sym):
            tags.append("日K趋势向上")
    except (IndexError, KeyError, ValueError):
        pass

    auto_conditions = evaluate_auto_trade_conditions(
        sym,
        market_cap_info,
        min_market_cap=float(auto_trade_cfg.get("market_cap_min", 5_000_000)),
        max_market_cap=float(auto_trade_cfg.get("market_cap_max", 1_000_000_000)),
        min_quote_volume=float(auto_trade_cfg.get("min_quote_volume_1d", 500_000)),
    )
    tags.extend([tag for tag, ok in auto_conditions.items() if ok])

    anomaly_tf = detect_volume_anomaly(all_sym, key, "buy", anomaly_dict)
    if anomaly_tf:
        tags.append(f"成交量异动({anomaly_tf})")
    if check_anti_chase(sym, cfg):
        tags.append("长线未追高")
    if check_short_anti_chase(sym):
        tags.append("短线未追高")
    if check_ma60_up(sym):
        tags.append("MA60向上")
    if check_short_pullback(sym):
        tags.append("短期回调")

    if fund_rate < cfg.get("negative_funding_threshold", -0.05):
        tags.append(f"负费率({fund_rate * 100:.2f}%)")
    if is_not_rubbish(sym):
        tags.append("波动充足")
    if key in leading:
        tags.append("龙头币")
    if detect_bottom_volume_surge(sym):
        tags.append("底部放量")
    if detect_consolidation_breakout(sym, "1H"):
        tags.append("盘整突破")
    if detect_early_strong_trend(sym):
        tags.append("强势启动")

    return tags
