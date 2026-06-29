"""
异步数据获取模块：批量拉取 K 线数据、计算技术指标
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import aiohttp
import pandas as pd

from analysis.bollinger_bands import calculate_bollinger_bands
from analysis.atr import calculate_atr
from analysis.ma import moving_average_np
from analysis.macd import calculate_macd, calculate_ema
from analysis.rsi import calculate_rsi
from api.factory import get_exchange
from infra.config import get_config
from infra.env import NEED_PROXY, PROXIES
from infra.logger import log
from infra.util import get_time_ms

if TYPE_CHECKING:
    from models import AccountState

# 时间常量（毫秒）
MS_1D = 24 * 60 * 60 * 1000


async def _fetch_klines(session: aiohttp.ClientSession, params: dict,
                        semaphore: asyncio.Semaphore) -> list:
    """异步获取单个币种单个周期的 K 线数据，自动翻页"""
    from infra.env import EXCHANGE

    timestamp = int(get_time_ms())
    is_binance = EXCHANGE == "binance"
    async with semaphore:
        data: list = []
        ex = get_exchange()
        while True:
            url = ex.get_klines_url(
                params["symbol"], params["productType"],
                params["granularity"], params["limit"], str(timestamp),
            )
            kline_raw = None
            for attempt in range(5):
                try:
                    kwargs: dict = {"timeout": 10}
                    if NEED_PROXY:
                        kwargs["proxy"] = PROXIES["http"]
                    async with session.get(url, **kwargs) as resp:
                        text = await resp.text()
                    kline_raw = json.loads(text)
                    break
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    log.warning(
                        "K线请求重试 (%d/5) %s %s: %s",
                        attempt + 1, params["symbol"], params["granularity"], e,
                    )
                    await asyncio.sleep(2)

            if kline_raw is None:
                raise ConnectionError(
                    f"K线请求失败: {params['symbol']} {params['granularity']}"
                )

            # 统一格式: [timestamp, open, high, low, close, volume, quoteVolume]
            if is_binance:
                # Binance 返回: [[ts, o, h, l, c, vol, closeTime, quoteVol, ...]]
                kline_data = [
                    [str(k[0]), k[1], k[2], k[3], k[4], k[5], k[7]]
                    for k in kline_raw
                ] if isinstance(kline_raw, list) else []
            else:
                # Bitget 返回: {"data": [[ts, o, h, l, c, vol, quoteVol]]}
                kline_data = kline_raw.get("data", [])

            if not kline_data:
                log.debug("%s %s kline 返回为空", params["symbol"], params["granularity"])
                return [params["symbol"], params["granularity"], data]

            data = kline_data + data
            if len(data) >= int(params["limit"]):
                log.debug(
                    "%s %s data 达到 %s 根",
                    params["symbol"], params["granularity"], params["limit"],
                )
                return [params["symbol"], params["granularity"], data]

            timestamp = int(data[0][0]) - 1
            log.debug(
                "%s %s 时间戳回退: %s",
                params["symbol"], params["granularity"], timestamp,
            )


async def _batch_get(url_params: list[dict], max_concurrent: int = 10) -> list:
    """并发批量请求"""
    async with aiohttp.ClientSession() as session:
        sem = asyncio.Semaphore(max_concurrent)
        tasks = [_fetch_klines(session, p, sem) for p in url_params]
        return await asyncio.gather(*tasks, return_exceptions=True)


async def get_all_data(
    cycle_arr: list[str] | None = None,
    all_symbols: dict | None = None,
    key_list: list[str] | None = None,
    limit: str | None = None,
    state: "AccountState | None" = None,
) -> None:
    """
    获取所有币种的多周期 K 线数据

    :param cycle_arr:    周期列表
    :param all_symbols:  输出字典
    :param key_list:     指定币种列表
    :param limit:        K 线根数
    :param state:        账户状态（用于获取 ban 列表）
    """
    cfg = get_config()
    if cycle_arr is None:
        cycle_arr = cfg.get("default_cycles", ["1M", "1W", "1D", "4H", "1H", "15m"])
    if all_symbols is None:
        all_symbols = {}
    if key_list is None:
        key_list = []
    if limit is None:
        limit = cfg.get("default_kline_limit", "200")

    ban_list: list[str] = []
    ex = get_exchange()
    if not key_list:
        symbols = ex.get_all_symbol(ex.PRODUCT_TYPE)

        # 48小时内亏损的币不再开仓
        history = ex.get_history_position(ex.PRODUCT_TYPE, str(int(get_time_ms()) - 2 * MS_1D))
        log.debug("48小时内的历史仓位(需要ban掉)：%s", history)
        loss_ban = [p["symbol"] for p in history["data"]["list"] if float(p["netProfit"]) < 0]
        log.info("48小时内亏损的币(ban)：%s", loss_ban)

        # 2小时内平过仓的币进入冷却期
        MS_12H = 2 * 60 * 60 * 1000
        history_12h = ex.get_history_position(ex.PRODUCT_TYPE, str(int(get_time_ms()) - MS_12H))
        cooldown_ban = [p["symbol"] for p in history_12h["data"]["list"]]
        if cooldown_ban:
            log.info("12小时内平仓冷却(ban)：%s", cooldown_ban)

        position_keys = list(state.position.keys()) if state else []
        ban_stock = cfg.get("ban_stock_list", [])
        ban_stable = cfg.get("ban_stable_list", [])
        ban_list = position_keys + ban_stock + ban_stable
    else:
        symbols = {"data": [{"symbol": key} for key in key_list]}

    url_params = []
    for s in symbols["data"]:
        if s["symbol"] in ban_list:
            continue
        for cycle in cycle_arr:
            url_params.append({
                "symbol": s["symbol"],
                "productType": ex.PRODUCT_TYPE,
                "granularity": cycle,
                "limit": limit,
            })

    max_concurrent = cfg.get("max_concurrent_requests", 10)
    for i in range(0, len(url_params), max_concurrent):
        result = await _batch_get(url_params[i:i + max_concurrent], max_concurrent)
        for x in result:
            if isinstance(x, list) and isinstance(x[2], list):
                all_symbols.setdefault(x[0], {}).setdefault(x[1], {})
                all_symbols[x[0]][x[1]]["data"] = x[2]


def compute_indicators(all_sym: dict) -> None:
    """为所有币种的所有周期计算技术指标（布林带、MACD、均线、RSI、成交量震荡率）"""
    cfg = get_config()
    ma_periods = cfg.get("ma_periods", [5, 10, 15, 20, 30, 40, 60, 80, 120, 160, 200])
    atr_length = cfg.get("auto_trade", {}).get("atr_length", 14)
    for symbol in all_sym:
        for cycle in all_sym[symbol]:
            try:
                data = all_sym[symbol][cycle]["data"]
                closes = [float(x[4]) for x in data]
                all_sym[symbol][cycle]["bolling"] = calculate_bollinger_bands(closes)
                all_sym[symbol][cycle]["macd"] = calculate_macd(closes)
                if cycle == "1D":
                    all_sym[symbol][cycle]["atr"] = calculate_atr(data, atr_length)
                for period in ma_periods:
                    all_sym[symbol][cycle][f"ma{period}"] = moving_average_np(closes, period)

                # RSI 和成交量震荡率只在 1H 周期计算（盘整放量突破策略用）
                if cycle == "1H":
                    all_sym[symbol][cycle]["rsi"] = calculate_rsi(closes)
                    volumes = pd.Series([float(x[5]) for x in data])
                    if len(volumes) >= 26:
                        ema12 = calculate_ema(volumes, 12)
                        ema26 = calculate_ema(volumes, 26)
                        vol_osc = ((ema12 - ema26) / ema26 * 100).values.tolist()
                        all_sym[symbol][cycle]["volume_osc"] = vol_osc
            except (KeyError, IndexError, ValueError) as e:
                log.warning("指标计算异常 %s %s: %s", symbol, cycle, e)
