"""
做空策略（通过环境变量 SHORT_STRATEGY=true 切换启用）。

选币：复用做多策略已有的两个标签 —— 「龙头币」(core.scanner.find_leading_coins)
      与「小市值」(core.auto_strategy.evaluate_auto_trade_conditions)，
      取「非龙头 且 小市值」的交集，再随机挑 random_pick 个做空。
下单：保证金 = 总权益 * risk_per_trade，10x 杠杆，开空即挂交易所预设止盈单
      （价格下跌 take_profit_pct 自动平空止盈）。不设止损，用爆仓兜底。
持仓：同时最多 max_short_positions 个空单；预设止盈单在交易所侧自动触发。

本模块与做多策略完全独立，不改动原有做多代码。
"""
from __future__ import annotations

import asyncio
import random
import time
from decimal import Decimal
from time import sleep

from api.factory import get_exchange
from core.auto_strategy import evaluate_auto_trade_conditions
from core.data_fetcher import get_all_data, compute_indicators
from core.market_cap import get_market_cap_map, get_symbol_market_cap
from core.scanner import find_leading_coins
from core.short_order import short_order
# 复用做多编排里的通用校验/精度工具，避免逻辑漂移（不改动做多逻辑）
from core.live_trading import (
    _is_too_new, _has_no_data, _is_data_fresh,
    _contract_min_size_and_places, _round_size_down,
    _round_price_to_tick, _format_decimal,
)
from infra.config import get_config
from infra.logger import log
from infra.util import get_time_ms

MS_15M = 15 * 60 * 1000
MS_1D = 24 * 60 * 60 * 1000
SMALL_CAP_TAG = "小市值"


def _build_short_pool(all_sym: dict, state, cfg: dict, market_caps: dict) -> list[str]:
    """构建做空候选池：非龙头 且 小市值，且数据有效、未持仓、未冷却。"""
    short_cfg = cfg.get("short_trade", {})
    market_cap_max = float(short_cfg.get("market_cap_max", 1_000_000_000))

    leading = set(find_leading_coins(all_sym))
    old_data_symbols: dict = {"15m": [], "1H": [], "4H": [], "1D": [], "1W": []}

    pool: list[str] = []
    for key, sym in all_sym.items():
        if key in state.position or key == "BTCUSDT":
            continue
        if key in state.cooldown:
            continue
        if key in leading:  # 复用「龙头币」标签 → 取非龙头
            continue
        if _is_too_new(sym) or _has_no_data(sym):
            continue
        if not _is_data_fresh(sym, key, old_data_symbols):
            continue

        # 复用做多的「小市值」标签判定
        conditions = evaluate_auto_trade_conditions(
            sym,
            get_symbol_market_cap(key, market_caps),
            max_market_cap=market_cap_max,
        )
        if not conditions.get(SMALL_CAP_TAG):
            continue

        pool.append(key)

    return pool


def scan_market_short(state) -> tuple[dict, list[str]]:
    """扫描全市场，返回 (all_sym, 做空候选池)。"""
    cfg = get_config()
    short_cfg = cfg.get("short_trade", {})
    cooldown_ms = int(float(cfg.get("rebuy_cooldown_hours", 2)) * 60 * 60 * 1000)

    ex = get_exchange()
    prev_position_keys = set(state.position.keys())
    all_position = ex.get_all_position(ex.PRODUCT_TYPE)
    state.position = {x["symbol"]: x for x in all_position["data"]}

    # 交易所侧预设止盈/爆仓会让持仓消失，补记冷却
    now_ms = int(get_time_ms())
    for vanished in prev_position_keys - set(state.position.keys()):
        state.cooldown.setdefault(vanished, now_ms)
    # 清理过期冷却
    state.cooldown = {
        s: t for s, t in state.cooldown.items() if now_ms - t < cooldown_ms
    }

    all_sym: dict = {}
    start_time = int(get_time_ms())
    cycles = ["1D", "4H", "1H", "15m"]
    asyncio.run(get_all_data(cycles, all_sym, state=state))
    if state.position:
        asyncio.run(get_all_data(cycles, all_sym, list(state.position.keys()), state=state))
    elapsed = (int(get_time_ms()) - start_time) / 1000
    log.info("做空：抓一遍所有币的数据，耗费时间：%.1fs", elapsed)

    compute_indicators(all_sym)

    try:
        market_caps = get_market_cap_map(
            ttl_seconds=int(short_cfg.get("market_cap_cache_ttl_seconds", 86400)),
            required_symbols=list(all_sym.keys()),
        )
    except Exception as exc:
        market_caps = {}
        log.warning("CoinGecko 市值数据不可用，本轮不产生做空候选: %s", exc)
        return all_sym, []

    pool = _build_short_pool(all_sym, state, cfg, market_caps)
    log.info("做空候选池（非龙头 且 小市值，共%d）：%s", len(pool), pool)
    return all_sym, pool


def _select_and_short(all_sym: dict, pool: list[str], state) -> None:
    """从候选池随机挑选并开空。"""
    cfg = get_config()
    short_cfg = cfg.get("short_trade", {})
    ex = get_exchange()

    max_positions = int(short_cfg.get("max_short_positions", 5))
    random_pick = int(short_cfg.get("random_pick", 5))
    risk_per_trade = float(short_cfg.get("risk_per_trade", 0.02))
    take_profit_pct = float(short_cfg.get("take_profit_pct", 0.03))
    leverage = 10

    slots = max_positions - len(state.position)
    if slots <= 0:
        log.info("已达最大空单持仓数 %d，停止开空", max_positions)
        return
    if not pool:
        log.info("做空候选：无")
        return

    acc = ex.get_accounts(ex.PRODUCT_TYPE)
    equity = float(acc["data"][0]["accountEquity"])
    state.update_balance(equity)
    if state.position_balance <= 0:
        log.warning("账户余额不足: %s，跳过开空", equity)
        return

    pick_n = min(random_pick, slots, len(pool))
    picks = random.sample(pool, pick_n)
    log.info("本轮随机挑选做空 %d 个：%s", pick_n, picks)

    for key in picks:
        if len(state.position) >= max_positions:
            log.info("已达最大空单持仓数 %d，停止开空", max_positions)
            break

        price = float(all_sym[key]["1D"]["data"][-1][4])
        if price <= 0:
            log.info("%s 价格异常，跳过", key)
            continue

        # 保证金口径：保证金 = 权益 * risk_per_trade，名义敞口 = 保证金 * 杠杆
        margin_need = equity * risk_per_trade
        notional = margin_need * leverage
        size = Decimal(str(notional / price))

        try:
            min_size, volume_place, price_tick = _contract_min_size_and_places(ex, key)
        except Exception as exc:
            log.warning("%s 获取交易所最小下单量失败，严格跳过: %s", key, exc)
            continue

        size = _round_size_down(size, volume_place)
        if size < min_size:
            log.info("%s 计算仓位 %s 小于交易所最小下单量 %s，跳过", key, size, min_size)
            continue

        # 校验交易所可开数量
        try:
            res = ex.open_count(
                key, ex.PRODUCT_TYPE, "USDT",
                str(margin_need), str(price), str(leverage),
            )
            exchange_size = Decimal(str(res["data"]["size"]))
            if exchange_size < size:
                size = _round_size_down(exchange_size, volume_place)
                if size < min_size:
                    log.info("%s 交易所可开数量 %s 小于最小下单量 %s，跳过", key, size, min_size)
                    continue
        except Exception as exc:
            log.warning("%s 查询可开数量失败，跳过: %s", key, exc)
            continue

        # 做空预设止盈价：开仓价下方 take_profit_pct
        tp_price = _round_price_to_tick(
            Decimal(str(price)) * (Decimal("1") - Decimal(str(take_profit_pct))),
            price_tick,
        )
        notional_actual = float(size) * price
        reason = f"做空：非龙头+小市值 随机挑选，跌{take_profit_pct * 100:.0f}%止盈"
        risk_info = {
            "strategy": "做空",
            "risk_per_trade": risk_per_trade,
            "take_profit_pct": take_profit_pct,
            "notional_usdt": notional_actual,
            "initial_margin_usdt": notional_actual / leverage,
        }
        log.info(
            "%s 开空: size=%s notional=%.2f margin=%.2f 止盈价=%s",
            key, size, notional_actual, notional_actual / leverage, _format_decimal(tp_price),
        )
        short_order(
            key, all_sym[key]["1D"]["data"], "OPEN", state,
            size=_format_decimal(size),
            reason=reason,
            bonus=["非龙头", SMALL_CAP_TAG],
            preset_take_profit=_format_decimal(tp_price),
            risk_info=risk_info,
        )


def short_strategy(state) -> None:
    """做空策略单次执行：同步持仓 → 扫描候选 → 随机开空补足持仓。"""
    all_sym, pool = scan_market_short(state)
    _select_and_short(all_sym, pool, state)


def _wait_until_next(minutes: int) -> None:
    interval = minutes * 60
    now = int(time.time())
    remainder = now % interval
    if remainder != 0:
        sleep(interval - remainder)


def main_short() -> None:
    """做空策略主入口：每 scan_interval_minutes 分钟执行一次。

    止盈由交易所预设止盈单自动触发，无需盘中逐分钟监控。
    """
    from models import AccountState
    from infra.logger import log, notify
    from infra.env import EXCHANGE, BITGET_DEMO

    cfg = get_config()
    interval = cfg.get("scan_interval_minutes", 15)
    state = AccountState()

    log.info("%s 做空交易机器人启动", EXCHANGE.capitalize())
    if EXCHANGE == "bitget" and BITGET_DEMO:
        notify("⚠️ 当前为模拟盘模式（Demo Trading）· 做空策略")

    while True:
        try:
            short_strategy(state)
        except Exception as e:
            log.error("做空主循环异常: %s", e, exc_info=True)
            notify(f"做空主循环异常: {e}")
        _wait_until_next(interval)
