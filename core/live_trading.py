"""
=============================================================================
合约自动化交易机器人（实盘）— 主编排模块

支持交易所：Bitget / Binance（通过 EXCHANGE 环境变量切换）

职责：
    - 初始化 AccountState
    - 编排市场扫描 → 选币 → 下单 → 仓位监控的完整流程
    - 各子模块：strategy / scanner / order / position / data_fetcher
=============================================================================
"""
from __future__ import annotations

import asyncio
import time
from time import sleep

from api.factory import get_exchange
from infra.config import get_config
from core.data_fetcher import get_all_data, compute_indicators
from infra.logger import log, notify
from models import AccountState
from core.order import order
from core.position import cut_profit, track_price
from core.scanner import (
    detect_volume_anomaly, select_by_volume,
    select_by_volume_surge, find_fairy_guide, find_leading_coins,
    detect_consolidation_breakout, detect_early_strong_trend,
)
from core.strategy import (
    is_15m_trend_up, is_1h_trend_up, is_4h_trend_up, is_1d_trend_up,
    is_btc_trend_up, is_btc_trend_down, is_btc_12h_not_down,
)
from infra.util import get_time_ms
from core.copy_trading import report_copy_trading_status, report_history_summary

# 时间常量（毫秒）
MS_15M = 15 * 60 * 1000
MS_1D = 24 * 60 * 60 * 1000


# =============================================================================
#  数据校验与过滤
# =============================================================================

def _is_too_new(sym: dict) -> bool:
    """币种数据是否太少（K 线不足 20 根）"""
    try:
        for tf in ("4H", "1H", "15m"):
            if tf not in sym or len(sym[tf].get("data") or []) < 20:
                return True
        return False
    except (KeyError, TypeError) as e:
        log.warning("_is_too_new 异常: %s", e)
        return True


def _is_rubbish(sym: dict) -> bool:
    """连续三天振幅小于 10% 的低波动币"""
    for i in range(-3, 0):
        if float(sym["1D"]["data"][i][2]) > float(sym["1D"]["data"][i][3]) * 1.1:
            return False
    return True


def _has_no_data(sym: dict) -> bool:
    """是否存在空数据的周期"""
    return any(len(sym[tf]["data"]) <= 0 for tf in ("1D", "4H", "1H", "15m"))


def _is_data_fresh(sym: dict, key: str, old_data_symbols: dict) -> bool:
    """检查各周期数据是否足够新"""
    try:
        now = int(get_time_ms())
        freshness = {"15m": MS_15M, "1H": 60 * 60 * 1000, "4H": 4 * 60 * 60 * 1000, "1D": MS_1D}
        for tf, max_age in freshness.items():
            if now - int(sym[tf]["data"][-1][0]) > max_age:
                old_data_symbols[tf].append(key)
                return False
        return True
    except (KeyError, IndexError, ValueError) as e:
        log.warning("_is_data_fresh 异常 %s: %s", key, e)
        return False


def _is_shutdown(state: AccountState) -> bool:
    """是否应该进入关停模式（回撤 > 10% 或 24 小时内爆仓超 2 次）"""
    cfg = get_config()
    if state.max_drawdown > cfg.get("max_drawdown_threshold", 0.1):
        return True
    try:
        ex = get_exchange()
        fill_history = ex.get_fill_history(ex.PRODUCT_TYPE, int(get_time_ms()) - MS_1D)
        burst_count = 0
        fill_list = fill_history.get("data", {}).get("fillList")
        if fill_list:
            burst_count = sum(
                1 for x in fill_list
                if x["tradeSide"] in ("burst_close_long", "burst_close_short")
            )
        return burst_count > cfg.get("max_burst_count", 2)
    except (KeyError, TypeError) as e:
        log.warning("_is_shutdown 检查异常: %s", e)
        return False


def _min_price_7d(sym: dict) -> float:
    """近 7 日最低价"""
    days = min(7, len(sym["1D"]["data"]))
    return min(float(sym["1D"]["data"][-i][3]) for i in range(1, days + 1))


# =============================================================================
#  选币下单
# =============================================================================

def _select_and_order(all_sym: dict, state: AccountState) -> None:
    """根据 buy_list 执行下单，加分项越多优先级越高"""
    cfg = get_config()
    ex = get_exchange()

    if not state.buy_list:
        log.info("可以开多的币：无")
        return

    # 按加分项数量降序排序，过滤掉没有加分项的币
    sorted_keys = sorted(
        [k for k in state.buy_list if state.buy_list[k]["bonus"]],
        key=lambda k: len(state.buy_list[k]["bonus"]),
        reverse=True,
    )

    # 先通知所有候选币（含无加分的），再只对有加分的下单
    labels = []
    for k in sorted(state.buy_list, key=lambda k: len(state.buy_list[k]["bonus"]), reverse=True):
        bonus = state.buy_list[k]["bonus"]
        tag = f"{k}({', '.join(bonus)})" if bonus else f"{k}(无加分,跳过)"
        labels.append(tag)
    log.info("可以开多的币（按优先级）：%s", '; '.join(labels))

    if not sorted_keys:
        log.info("没有带加分项的币，跳过开仓")
        return

    acc = ex.get_accounts(ex.PRODUCT_TYPE)
    equity = float(acc["data"][0]["accountEquity"])
    state.update_balance(equity)
    if state.position_balance <= 0:
        log.warning("⚠️ 账户余额不足: %s，跳过下单", equity)
        return

    for key in sorted_keys:
        cur_price = all_sym[key]["15m"]["data"][-1][4]
        res = ex.open_count(
            key, ex.PRODUCT_TYPE, "USDT",
            str(state.position_balance), cur_price,
            str(cfg.get("leverage", 10)),
        )
        log.info("币种：%s 可开数量：%s", key, res['data']['size'])

        min_size = state.position_balance * 0.1 / float(cur_price)
        if float(res["data"]["size"]) / 2 < min_size:
            log.info("可开数量不足")
            continue

        max_positions = cfg.get("max_long_positions", 3)
        if len(state.position) >= max_positions:
            log.info("已达最大持仓数 %d，停止开仓", max_positions)
            break
        buy_info = state.buy_list[key]
        order(key, all_sym[key]["15m"]["data"], "BUY", state,
              reason=buy_info.get("reason", ""),
              bonus=buy_info.get("bonus", []))


# =============================================================================
#  市场扫描
# =============================================================================

def scan_market(state: AccountState, is_four_hour: bool = False) -> dict:
    """
    扫描全市场，筛选符合策略条件的币种
    核心策略：成交量异动 + 多周期趋势共振
    """
    cfg = get_config()

    # 每次全市场扫描前，从服务器同步一次持仓到内存
    ex = get_exchange()
    all_position = ex.get_all_position(ex.PRODUCT_TYPE)
    state.position = {x["symbol"]: x for x in all_position["data"]}
    state.buy_list = {}
    all_sym: dict = {}

    start_time = int(get_time_ms())
    asyncio.run(get_all_data(["1D", "4H", "1H", "15m"], all_sym, state=state))
    elapsed = (int(get_time_ms()) - start_time) / 1000
    log.info("抓一遍所有币的数据，耗费时间：%.1fs", elapsed)

    compute_indicators(all_sym)

    all_keys: list[str] = []
    trend_up_symbols: list[str] = []
    valid_symbols: list[str] = []
    new_symbols: list[str] = []
    no_data_symbols: list[str] = []
    old_data_symbols: dict = {"15m": [], "1H": [], "4H": [], "1D": []}
    volume_anomaly: dict = {"15m": [], "1H": [], "4H": []}

    max_7d = cfg.get("max_7d_gain_mult", 2.7)
    max_boll = cfg.get("max_boll_width_mult", 2.7)
    max_upper = cfg.get("max_close_above_upper_mult", 1.1)

    for key in all_sym:
        all_keys.append(key)
        sym = all_sym[key]

        if _is_too_new(sym):
            new_symbols.append(key)
            continue
        if _has_no_data(sym):
            log.debug("存在空数据的币：%s", key)
            no_data_symbols.append(key)
            continue
        if not _is_data_fresh(sym, key, old_data_symbols):
            continue
        if key == "BTCUSDT":
            continue

        valid_symbols.append(key)

        # ---- 策略：BTC大盘方向 + 多周期趋势共振 + 波动充足 + 未追高 ----
        trend_all_up = (
            is_15m_trend_up(sym, "15m")
            and is_1h_trend_up(sym, "1H")
            and is_4h_trend_up(sym, "4H")
            and is_1d_trend_up(sym)
        )
        # 防追高
        close_price = float(sym["1D"]["data"][-1][4])
        not_overextended = (
            close_price < _min_price_7d(sym) * max_7d
            and sym["1D"]["bolling"]["Upper Band"][-1]
            < sym["1D"]["bolling"]["Lower Band"][-1] * max_boll
        )
        not_above_upper = close_price < sym["1D"]["bolling"]["Upper Band"][-1] * max_upper
        btc_ok = is_btc_12h_not_down(all_sym)

        if trend_all_up:
            trend_up_symbols.append(key)
            # 调试：趋势共振币逐条件打印
            log.info(
                "%s 条件检查: btc_ok=%s not_overextended=%s "
                "not_above_upper=%s not_rubbish=%s",
                key, btc_ok, not_overextended, not_above_upper, not _is_rubbish(sym),
            )

        # 四条件组合即可开仓，不再要求成交量异动
        if (trend_all_up and not_overextended and not_above_upper
                and btc_ok and not _is_rubbish(sym)):
            state.buy_list[key] = {"reason": "趋势共振 + 波动充足 + 未追高", "bonus": []}

        # 成交量异动检测，有异动的币标记加分
        anomaly_tf = detect_volume_anomaly(all_sym, key, "buy", volume_anomaly)
        if anomaly_tf:
            log.info("🔔 %s 出现 %s 成交量异动", key, anomaly_tf)
            if key in state.buy_list:
                state.buy_list[key]["bonus"].append(f"成交量异动({anomaly_tf})")

    # ---- 对 buy_list 候选币做加分项检测 ----
    if state.buy_list:
        # 龙头币
        leading = set(find_leading_coins(all_sym))
        for key in state.buy_list:
            if key in leading:
                state.buy_list[key]["bonus"].append("龙头币")

        # 仙人指路
        fairy = set(find_fairy_guide(all_sym, state))
        for key in state.buy_list:
            if key in fairy:
                state.buy_list[key]["bonus"].append("仙人指路")

        # 负资金费率
        ex = get_exchange()
        for key in state.buy_list:
            try:
                fund_rate = ex.get_history_fund_rate(key, ex.PRODUCT_TYPE)
                total = sum(float(x["fundingRate"]) for x in fund_rate["data"])
                if total < -0.05:
                    state.buy_list[key]["bonus"].append(f"负费率({total*100:.2f}%)")
            except Exception as e:
                log.warning("获取 %s 资金费率异常: %s", key, e)

        # 盘整放量突破（1H 周期）
        for key in state.buy_list:
            if detect_consolidation_breakout(all_sym.get(key, {}), "1H"):
                state.buy_list[key]["bonus"].append("盘整放量突破")
                log.info("🔔 %s 出现 1H 盘整放量突破信号", key)

        # 强势启动（4H MA多头排列加速）
        for key in state.buy_list:
            if detect_early_strong_trend(all_sym.get(key, {})):
                state.buy_list[key]["bonus"].append("强势启动")
                log.info("🔔 %s 出现 4H 强势启动信号", key)

    if trend_up_symbols:
        log.info("多头趋势币(%d)：%s", len(trend_up_symbols), ', '.join(trend_up_symbols))

    log.info(
        "扫描完成，全部交易对：%d 可分析：%d 新币:%s 空数据:%s 数据旧:%s",
        len(all_keys), len(valid_symbols), new_symbols, no_data_symbols, old_data_symbols,
    )
    return all_sym


# =============================================================================
#  仓位扫描与主循环
# =============================================================================

def _scan_position(state: AccountState) -> None:
    """扫描当前持仓（基于内存），获取数据并执行止盈逻辑"""
    key_list = list(state.position.keys())

    if not key_list:
        return

    all_sym: dict = {}
    is_first = state.is_first_scan_position
    if is_first:
        # 根据最早开仓时间动态计算 15m K线需要多少根，确保覆盖整个持仓周期
        earliest_ctime = min(int(x["cTime"]) for x in state.position.values())
        hold_ms = int(get_time_ms()) - earliest_ctime
        limit_15m = max(300, int(hold_ms / MS_15M) + 10)
        limit = str(limit_15m)
        log.info("首次扫描持仓，最早开仓距今 %.1f 天，15m limit=%s",
                 hold_ms / MS_1D, limit)
    else:
        limit = "41"
    asyncio.run(get_all_data(["1D", "15m", "1m"], all_sym, key_list, limit, state))
    if is_first:
        state.is_first_scan_position = False

    track_price(all_sym, is_first, state)
    compute_indicators(all_sym)

    for key in all_sym:
        cut_profit(key, all_sym[key], state, order)


def _full_scan_and_order(state: AccountState, is_four_hour: bool = False) -> dict:
    """执行完整的市场扫描 + 下单 + 辅助分析"""
    all_sym = scan_market(state, is_four_hour)

    # 小成交量+不错涨幅，作为加分项
    small_vol = set(select_by_volume(all_sym, state))
    for key in small_vol:
        if key in state.buy_list:
            state.buy_list[key]["bonus"].append("小成交量+不错涨幅")

    _select_and_order(all_sym, state)
    select_by_volume_surge(all_sym, state)
    return all_sym


def _loop_scan_position(state: AccountState) -> None:
    """持仓期间的循环监控，使用内存中的持仓数据，不再轮询 API"""
    _scan_position(state)
    while True:
        _wait_until_next(1)
        if not state.position:
            break
        _scan_position(state)

        now = int(time.time())
        if now % (4 * 3600) <= 60:
            _full_scan_and_order(state, is_four_hour=True)
            # 带单模式：每 4 小时汇报历史带单收益
            if get_config().get("copy_trading_enabled", False):
                report_history_summary()
        elif now % (15 * 60) <= 60:
            _full_scan_and_order(state)


def strategy(state: AccountState) -> None:
    """单次策略执行：更新余额 → 检查持仓 → 扫描市场 → 下单"""
    cfg = get_config()
    ex = get_exchange()
    acc = ex.get_accounts(ex.PRODUCT_TYPE)
    state.update_balance(float(acc["data"][0]["accountEquity"]))

    # 带单模式：汇报当前带单状态
    if cfg.get("copy_trading_enabled", False):
        report_copy_trading_status()

    if state.position:
        _loop_scan_position(state)
    else:
        _full_scan_and_order(state)
        if state.position:
            _loop_scan_position(state)

    # 更新空仓时间
    if not state.position:
        state.no_position_time += MS_15M
    else:
        state.long_position_time += MS_15M


def _wait_until_next(minutes: int) -> None:
    """等待到下一个整分钟"""
    interval = minutes * 60
    now = int(time.time())
    remainder = now % interval
    if remainder != 0:
        sleep(interval - remainder)


def main() -> None:
    """主入口：每 15 分钟执行一次策略"""
    cfg = get_config()
    interval = cfg.get("scan_interval_minutes", 15) * 60
    state = AccountState()

    from infra.env import EXCHANGE
    log.info("%s 交易机器人启动", EXCHANGE.capitalize())
    from infra.env import BITGET_DEMO
    if EXCHANGE == "bitget" and BITGET_DEMO:
        notify("⚠️ 当前为模拟盘模式（Demo Trading）")
    while True:
        try:
            strategy(state)
            now = int(time.time())
            remainder = now % interval
            if remainder != 0:
                sleep(interval - remainder + 1)
        except Exception as e:
            log.error("主循环异常: %s", e, exc_info=True)
            notify(f"主循环异常: {e}")
