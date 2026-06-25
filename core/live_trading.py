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
from decimal import Decimal, ROUND_DOWN
from time import sleep

from api.factory import get_exchange
from infra.env import EXCHANGE
from infra.config import get_config
from core.data_fetcher import get_all_data, compute_indicators
from infra.logger import log, notify
from models import AccountState
from core.order import order
from core.margin import estimate_extra_isolated_margin
from core.position import cut_profit, track_price
from core.auto_strategy import (
    AUTO_TRADE_TAG,
    build_auto_trade_reason,
    compute_trade_risk,
)
from core.market_cap import get_market_cap_map, get_symbol_market_cap
from core.risk_cache import load_position_risk
from core.scanner import (
    detect_volume_anomaly, select_by_volume,
    find_fairy_guide, find_leading_coins,
    detect_consolidation_breakout, detect_early_strong_trend,
)
from core.strategy import (
    is_15m_trend_up, is_1h_trend_up, is_4h_trend_up, is_1d_trend_up,
    is_btc_12h_not_down,
)
from core.tagging import build_symbol_tags
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


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _contract_min_size_and_places(ex, symbol: str) -> tuple[Decimal, int, Decimal]:
    contracts = ex.get_contracts(symbol, ex.PRODUCT_TYPE)
    data = contracts.get("data") or []
    info = data[0] if isinstance(data, list) and data else data
    if not isinstance(info, dict):
        raise ValueError("empty contract info")
    min_size = Decimal(str(info.get("minTradeNum", "0")))
    volume_place = int(info.get("volumePlace", 8))
    # 价格最小变动单位：pricePlace 为小数位数，priceEndStep 为步进倍数
    price_place = int(info.get("pricePlace", 8))
    price_end_step = Decimal(str(info.get("priceEndStep", "1")))
    price_tick = price_end_step * Decimal("1").scaleb(-price_place)
    return min_size, volume_place, price_tick


def _round_size_down(size: Decimal, volume_place: int) -> Decimal:
    quantum = Decimal("1").scaleb(-volume_place)
    return size.quantize(quantum, rounding=ROUND_DOWN)


def _round_price_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """将价格向下取整到交易所价格最小变动单位的整数倍。"""
    if tick <= 0:
        return price
    return (price / tick).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick


def _estimate_position_risk(symbol: str, pos: dict, all_sym: dict,
                            risk_cache: dict, cfg: dict) -> float:
    cached = risk_cache.get(symbol)
    if cached and cached.get("actual_risk_usdt") is not None:
        return float(cached["actual_risk_usdt"])

    sym = all_sym.get(symbol)
    if not sym:
        log.warning("%s 无法估算已有持仓风险：缺少K线，按0计入", symbol)
        return 0.0
    try:
        day = sym["1D"]
        bb_mid = float(day["bolling"]["Middle Band"][-1])
        atr = float(day["atr"][-1])
        stop_price = bb_mid - atr * cfg.get("atr_stop_multi", 1.2)
        open_price = float(pos.get("openPriceAvg", 0))
        size = float(pos.get("available", pos.get("total", 0)))
        return max(open_price - stop_price, 0) * size
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        log.warning("%s 估算已有持仓风险失败: %s", symbol, exc)
        return 0.0


def _current_total_risk(state: AccountState, all_sym: dict, cfg: dict) -> float:
    risk_cache = load_position_risk()
    total = 0.0
    for symbol, pos in state.position.items():
        if pos.get("holdSide") != "long":
            continue
        total += _estimate_position_risk(symbol, pos, all_sym, risk_cache, cfg)
    return total


# =============================================================================
#  选币下单
# =============================================================================

def _select_and_order(all_sym: dict, state: AccountState) -> None:
    """按自动交易策略执行下单。旧加分项不参与真实交易。"""
    cfg = get_config()
    auto_cfg = cfg.get("auto_trade", {})
    ex = get_exchange()

    if not state.buy_list:
        log.info("自动交易候选：无")
        return

    # 按标签数量降序排列，标签数相同则用原 score（低位优先）做次级排序
    sorted_keys = sorted(
        state.buy_list,
        key=lambda k: (-state.buy_list[k].get("tag_count", 0), state.buy_list[k]["signal"].score),
    )
    log.info(
        "自动交易候选（按标签数量降序）：%s",
        ", ".join(f"{k}({state.buy_list[k].get('tag_count', 0)})" for k in sorted_keys),
    )

    acc = ex.get_accounts(ex.PRODUCT_TYPE)
    equity = float(acc["data"][0]["accountEquity"])
    state.update_balance(equity)
    if state.position_balance <= 0:
        log.warning("账户余额不足: %s，跳过下单", equity)
        return

    risk_per_trade = float(auto_cfg.get("risk_per_trade", 0.02))
    max_total_risk = float(auto_cfg.get("max_total_risk", 0.10))
    max_symbol_notional_pct = float(auto_cfg.get("max_symbol_notional_pct", 0.20))
    leverage = 10 if EXCHANGE == "bitget" else int(cfg.get("leverage", 10))
    current_risk = _current_total_risk(state, all_sym, auto_cfg)
    log.info(
        "当前总风险占用: %.4f / %.4f USDT (%.2f%% / %.2f%%)",
        current_risk, equity * max_total_risk,
        current_risk / equity * 100 if equity else 0,
        max_total_risk * 100,
    )

    for key in sorted_keys:
        max_positions = cfg.get("max_long_positions", 3)
        if len(state.position) >= max_positions:
            log.info("已达最大持仓数 %d，停止开仓", max_positions)
            break

        buy_info = state.buy_list[key]
        signal = buy_info["signal"]
        if current_risk >= equity * max_total_risk:
            log.info("总风险占用已达到 %.0f%%，停止新开仓", max_total_risk * 100)
            break

        stop_distance = signal.close - signal.stop_price
        if stop_distance <= 0:
            log.info("%s 止损距离异常，跳过", key)
            continue

        risk_budget = min(equity * risk_per_trade, equity * max_total_risk - current_risk)
        risk_size = Decimal(str(risk_budget / stop_distance))
        max_notional_size = Decimal(str((equity * max_symbol_notional_pct) / signal.close))
        size = min(risk_size, max_notional_size)

        try:
            min_size, volume_place, price_tick = _contract_min_size_and_places(ex, key)
        except Exception as exc:
            log.warning("%s 获取交易所最小下单量失败，严格跳过: %s", key, exc)
            continue

        size = _round_size_down(size, volume_place)
        if size < min_size:
            log.info("%s 计算仓位 %s 小于交易所最小下单量 %s，跳过", key, size, min_size)
            continue

        notional = float(size) * signal.close
        planned_risk = float(size) * stop_distance
        margin_need = notional / leverage if leverage else notional
        try:
            res = ex.open_count(
                key, ex.PRODUCT_TYPE, "USDT",
                str(margin_need), str(signal.close), str(leverage),
            )
            exchange_size = Decimal(str(res["data"]["size"]))
            if exchange_size < size:
                size = _round_size_down(exchange_size, volume_place)
                planned_risk = float(size) * stop_distance
                notional = float(size) * signal.close
                if size < min_size:
                    log.info("%s 交易所可开数量 %s 小于最小下单量 %s，跳过", key, size, min_size)
                    continue
        except Exception as exc:
            log.warning("%s 查询可开数量失败，跳过: %s", key, exc)
            continue

        if current_risk + planned_risk > equity * max_total_risk:
            log.info("%s 开仓后总风险超限，跳过", key)
            continue

        margin_plan = None
        if leverage > 1 and EXCHANGE != "bitget":
            margin_plan = estimate_extra_isolated_margin(
                signal.close, float(size), signal.stop_price, leverage, equity, auto_cfg,
            )
            if margin_plan.capped:
                log.info(
                    "%s required extra margin for ATR stop protection is above cap, skip: %.2f USDT",
                    key, margin_plan.required_margin - margin_plan.initial_margin,
                )
                continue

        reason = buy_info.get("reason") or build_auto_trade_reason(signal)
        risk_info = {
            "strategy": AUTO_TRADE_TAG,
            "planned_risk_usdt": planned_risk,
            "risk_per_trade": risk_per_trade,
            "total_risk_before_usdt": current_risk,
            "stop_price": signal.stop_price,
            "atr": signal.atr,
            "bb_mid": signal.bb_mid,
            "market_cap": signal.market_cap,
            "market_cap_source": signal.market_cap_source,
            "notional_usdt": notional,
            "estimated_extra_margin_usdt": margin_plan.extra_margin if margin_plan else 0.0,
            "target_liquidation_price": margin_plan.target_liquidation_price if margin_plan else 0.0,
        }
        matched_tags = buy_info.get("tags", [])
        log.info(
            "%s 自动交易开仓: 标签数=%d [%s] size=%s notional=%.2f risk=%.2f stop=%.6g market_cap=%.0f source=%s",
            key, len(matched_tags), ", ".join(matched_tags),
            size, notional, planned_risk, signal.stop_price,
            signal.market_cap, signal.market_cap_source.get("id"),
        )
        stop_price_rounded = _round_price_to_tick(
            Decimal(str(signal.stop_price)), price_tick
        )
        result = order(
            key, all_sym[key]["1D"]["data"], "BUY", state,
            reason=reason, bonus=buy_info.get("bonus", []),
            size=_format_decimal(size),
            preset_stop_loss=_format_decimal(stop_price_rounded),
            risk_info=risk_info,
        )
        if result is not None:
            current_risk += planned_risk


# =============================================================================
#  市场扫描
# =============================================================================

def _legacy_scan_market(state: AccountState, is_four_hour: bool = False) -> dict:
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

        # ---- 策略：BTC大盘方向 + 多周期趋势共振 + 波动充足 + 长线未追高 ----
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
            state.buy_list[key] = {"reason": "趋势共振 + 波动充足 + 长线未追高", "bonus": []}

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


def scan_market(state: AccountState, is_four_hour: bool = False) -> dict:
    """Scan the market and build auto-trading candidates only."""
    cfg = get_config()
    auto_cfg = cfg.get("auto_trade", {})
    cooldown_ms = int(float(cfg.get("rebuy_cooldown_hours", 2)) * 60 * 60 * 1000)

    ex = get_exchange()
    prev_position_keys = set(state.position.keys())
    all_position = ex.get_all_position(ex.PRODUCT_TYPE)
    state.position = {x["symbol"]: x for x in all_position["data"]}
    # 交易所侧触发的止损（ATR 预设止损）会让持仓消失，但不经过 close_position，
    # 这里补记冷却，确保止损后也进入冷却期。
    now_ms = int(get_time_ms())
    for vanished in prev_position_keys - set(state.position.keys()):
        state.cooldown.setdefault(vanished, now_ms)
    state.buy_list = {}
    all_sym: dict = {}

    start_time = int(get_time_ms())
    asyncio.run(get_all_data(["1W", "1D", "4H", "1H", "15m"], all_sym, state=state))
    if state.position:
        asyncio.run(get_all_data(["1D"], all_sym, list(state.position.keys()), state=state))
    elapsed = (int(get_time_ms()) - start_time) / 1000
    log.info("抓一遍所有币的数据，耗费时间：%.1fs", elapsed)

    compute_indicators(all_sym)

    try:
        market_caps = get_market_cap_map(
            ttl_seconds=int(auto_cfg.get("market_cap_cache_ttl_seconds", 86400)),
            required_symbols=list(all_sym.keys()),
        )
    except Exception as exc:
        market_caps = {}
        log.warning("CoinGecko 市值数据不可用，本轮不产生自动交易候选: %s", exc)

    all_keys: list[str] = []
    valid_symbols: list[str] = []
    new_symbols: list[str] = []
    no_data_symbols: list[str] = []
    old_data_symbols: dict = {"15m": [], "1H": [], "4H": [], "1D": [], "1W": []}

    leading = set(find_leading_coins(all_sym))
    volume_anomaly: dict = {"15m": [], "1H": [], "4H": []}

    # 清理已过期的冷却记录
    state.cooldown = {
        s: t for s, t in state.cooldown.items() if now_ms - t < cooldown_ms
    }
    cooldown_skipped: list[str] = []

    for key, sym in all_sym.items():
        all_keys.append(key)
        if key in state.position or key == "BTCUSDT":
            continue
        if key in state.cooldown:
            cooldown_skipped.append(key)
            continue
        if _is_too_new(sym):
            new_symbols.append(key)
            continue
        if _has_no_data(sym):
            no_data_symbols.append(key)
            continue
        if not _is_data_fresh(sym, key, old_data_symbols):
            continue

        valid_symbols.append(key)
        # 按标签数量准入：先算出能否安全下单所需的 ATR 止损/仓位参数，
        # 无法安全下单的币（数据不足/ATR或止损无效）跳过。
        risk = compute_trade_risk(
            key,
            sym,
            get_symbol_market_cap(key, market_caps),
            atr_min=float(auto_cfg.get("atr_min", 0.001)),
            atr_stop_multi=float(auto_cfg.get("atr_stop_multi", 1.2)),
        )
        if risk is None:
            continue

        # 组装全部选币标签（负费率需要逐币 API，先不计，稍后对候选做补充）
        tags = build_symbol_tags(
            all_sym, key, sym, cfg,
            market_cap_info=get_symbol_market_cap(key, market_caps),
            fund_rate=0.0,
            leading=leading,
            anomaly_dict=volume_anomaly,
        )
        state.buy_list[key] = {
            "signal": risk,
            "tags": tags,
        }

    # ---- 对候选集补充需要候选集才能算的标签：小量大涨 / 仙人指路 ----
    if state.buy_list:
        low_vol = set(select_by_volume(all_sym, state))
        fairy = set(find_fairy_guide(all_sym, state))
        for k in state.buy_list:
            if k in low_vol:
                state.buy_list[k]["tags"].append("小量大涨")
            if k in fairy:
                state.buy_list[k]["tags"].append("仙人指路")

        # ---- 负费率：逐币 API 太慢，只对标签最多的候选 shortlist 补充 ----
        max_positions = cfg.get("max_long_positions", 3)
        shortlist_n = max(3 * max_positions, 12)
        shortlist = sorted(
            state.buy_list,
            key=lambda k: len(state.buy_list[k]["tags"]),
            reverse=True,
        )[:shortlist_n]
        threshold = cfg.get("negative_funding_threshold", -0.05)
        for k in shortlist:
            try:
                fund_rate = ex.get_history_fund_rate(k, ex.PRODUCT_TYPE)
                total = sum(float(x["fundingRate"]) for x in fund_rate["data"])
                if total < threshold:
                    state.buy_list[k]["tags"].append(f"负费率({total * 100:.2f}%)")
            except Exception as exc:
                log.warning("获取 %s 资金费率异常: %s", k, exc)

        # ---- 最终：写入 reason / bonus（标签）/ tag_count ----
        for k, info in state.buy_list.items():
            tag_list = info["tags"]
            info["tag_count"] = len(tag_list)
            info["bonus"] = tag_list
            info["reason"] = f"按标签数量选币（命中 {len(tag_list)} 个标签）"

        ranked = sorted(
            state.buy_list,
            key=lambda k: len(state.buy_list[k]["tags"]),
            reverse=True,
        )
        log.info(
            "自动交易候选（按标签数量降序，共%d）：%s",
            len(ranked),
            ", ".join(f"{k}({len(state.buy_list[k]['tags'])})" for k in ranked[:shortlist_n]),
        )

    log.info(
        "扫描完成，全部交易对:%d 可分析:%d 自动交易候选:%d 冷却跳过:%s 新币:%s 空数据:%s 数据旧:%s",
        len(all_keys), len(valid_symbols), len(state.buy_list),
        cooldown_skipped, new_symbols, no_data_symbols, old_data_symbols,
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
    """执行完整的自动交易扫描 + 下单。"""
    all_sym = scan_market(state, is_four_hour)
    _select_and_order(all_sym, state)
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
