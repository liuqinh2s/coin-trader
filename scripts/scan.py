"""
Single-run market scanner for the static dashboard.

This entrypoint fetches public Bitget futures market data, computes indicators,
then delegates all screening logic to the shared trading modules under core/.
It writes timestamped JSON files to a runtime data directory that is ignored by
git by default.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "runtime" / "scans"
CONFIG_PATH = ROOT / "config.local.json"

BITGET_API = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
CYCLES = ["1W", "1D", "4H", "1H", "15m"]
MS_15M = 15 * 60 * 1000
MS_1D = 24 * 60 * 60 * 1000

sys.path.insert(0, str(ROOT))

from core.data_fetcher import compute_indicators  # noqa: E402
from core.auto_strategy import evaluate_auto_trade_signal  # noqa: E402
from core.market_cap import get_market_cap_map, get_symbol_market_cap  # noqa: E402
from infra.config import get_config  # noqa: E402
from core.scanner import (  # noqa: E402
    find_fairy_guide,
    find_leading_coins,
    select_by_volume,
)
from core.tagging import build_symbol_tags  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scan")


def _load_local_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.warning("config.local.json 解析失败: %s", exc)
        return {}


def _get_proxy_url(cfg: dict[str, Any]) -> str | None:
    proxy_url = os.environ.get("PROXY_URL")
    if proxy_url:
        log.info("已启用代理 (环境变量): %s", proxy_url)
        return proxy_url
    proxy_cfg = cfg.get("proxy", {})
    if proxy_cfg.get("enabled"):
        proxy_url = f"http://{proxy_cfg['host']}:{proxy_cfg['port']}"
        log.info("已启用代理 (本地配置): %s", proxy_url)
        return proxy_url
    return None


def _get_data_dir(cfg: dict[str, Any]) -> Path:
    raw = os.environ.get("SCAN_DATA_DIR") or cfg.get("data_dir")
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else ROOT / path
    return DEFAULT_DATA_DIR


async def fetch_json(session: aiohttp.ClientSession, url: str, proxy_url: str | None) -> Any:
    for attempt in range(5):
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with session.get(url, proxy=proxy_url, timeout=timeout) as resp:
                if resp.status == 200:
                    return await resp.json()
                log.warning("HTTP %d: %s", resp.status, url[:100])
        except Exception as exc:
            log.warning("请求失败 (%d/5) %s: %s", attempt + 1, url[:80], exc)
        await asyncio.sleep(2 * (attempt + 1))
    return None


async def fetch_all_symbols(session: aiohttp.ClientSession, proxy_url: str | None) -> list[str]:
    url = f"{BITGET_API}/api/v2/mix/market/tickers?productType={PRODUCT_TYPE}"
    data = await fetch_json(session, url, proxy_url)
    if not data or data.get("code") != "00000" or not data.get("data"):
        log.error("tickers 返回异常: %s", str(data)[:200] if data else "None")
        return []
    symbols = [item["symbol"] for item in data["data"]]
    log.info("获取到 %d 个 USDT 永续合约", len(symbols))
    return symbols


async def fetch_klines(
    session: aiohttp.ClientSession,
    symbol: str,
    granularity: str,
    proxy_url: str | None,
    limit: int = 200,
) -> tuple[str, str, list]:
    rows: list = []
    end_time: int | None = None
    seen_timestamps: set[str] = set()
    page_limit = min(limit, 200)

    while len(rows) < limit:
        url = (
            f"{BITGET_API}/api/v2/mix/market/candles"
            f"?symbol={symbol}&productType={PRODUCT_TYPE}"
            f"&granularity={granularity}&limit={page_limit}"
        )
        if end_time is not None:
            url += f"&endTime={end_time}"

        data = await fetch_json(session, url, proxy_url)
        page = data.get("data", []) if data and data.get("code") == "00000" else []
        if not page:
            break

        new_rows = [
            row for row in page
            if row and str(row[0]) not in seen_timestamps
        ]
        if not new_rows:
            break
        seen_timestamps.update(str(row[0]) for row in new_rows)
        rows = new_rows + rows

        try:
            end_time = int(min(row[0] for row in new_rows)) - 1
        except (TypeError, ValueError):
            break

    try:
        rows.sort(key=lambda row: int(row[0]))
    except (TypeError, ValueError):
        pass
    return symbol, granularity, rows[-limit:]


async def fetch_history_fund_rates(
    session: aiohttp.ClientSession,
    symbols: list[str],
    proxy_url: str | None,
) -> dict[str, float]:
    sem = asyncio.Semaphore(10)
    result: dict[str, float] = {}

    async def _fetch_one(sym: str) -> None:
        async with sem:
            url = (
                f"{BITGET_API}/api/v2/mix/market/history-fund-rate"
                f"?symbol={sym}&productType={PRODUCT_TYPE}&pageSize=20"
            )
            data = await fetch_json(session, url, proxy_url)
            if data and data.get("code") == "00000" and data.get("data"):
                result[sym] = sum(float(x["fundingRate"]) for x in data["data"])

    await asyncio.gather(*[_fetch_one(symbol) for symbol in symbols], return_exceptions=True)
    return result


async def fetch_all_data(
    session: aiohttp.ClientSession,
    symbols: list[str],
    proxy_url: str | None,
    max_concurrent: int,
) -> dict[str, dict]:
    all_sym: dict[str, dict] = {}
    sem = asyncio.Semaphore(max_concurrent)

    async def _limited(sym: str, granularity: str) -> tuple[str, str, list]:
        async with sem:
            return await fetch_klines(session, sym, granularity, proxy_url)

    tasks = [_limited(sym, cycle) for sym in symbols for cycle in CYCLES]
    log.info("开始并发获取 K 线: %d 个请求, 并发上限 %d", len(tasks), max_concurrent)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for item in results:
        if isinstance(item, tuple) and len(item) == 3 and item[2]:
            sym, granularity, data = item
            all_sym.setdefault(sym, {})[granularity] = {"data": data}
    log.info("K 线获取完成: %d 个币种有数据", len(all_sym))
    return all_sym


def is_banned_symbol(symbol: str, cfg: dict[str, Any]) -> bool:
    return symbol in set(cfg.get("ban_stock_list", []) + cfg.get("ban_stable_list", []))


def is_too_new(sym: dict) -> bool:
    try:
        for tf in ("4H", "1H", "15m"):
            if tf not in sym or len(sym[tf].get("data") or []) < 20:
                return True
        return False
    except (KeyError, TypeError):
        return True


def has_no_data(sym: dict) -> bool:
    try:
        return any(len(sym[tf]["data"]) <= 0 for tf in ("1D", "4H", "1H", "15m"))
    except (KeyError, TypeError):
        return True


def is_data_fresh(sym: dict, key: str, old_data_symbols: dict) -> bool:
    try:
        now = int(time.time() * 1000)
        freshness = {"15m": MS_15M, "1H": 60 * 60 * 1000, "4H": 4 * 60 * 60 * 1000, "1D": MS_1D}
        for tf, max_age in freshness.items():
            if now - int(sym[tf]["data"][-1][0]) > max_age:
                old_data_symbols[tf].append(key)
                return False
        return True
    except (KeyError, IndexError, ValueError, TypeError):
        old_data_symbols.setdefault("unknown", []).append(key)
        return False


def has_required_indicators(sym: dict) -> bool:
    for tf in ("1D", "4H", "1H", "15m"):
        if tf not in sym or "bolling" not in sym[tf] or "macd" not in sym[tf]:
            return False
    return True


def cleanup_old_scans(data_dir: Path, retention_days: int) -> int:
    cutoff = time.time() - retention_days * 86400
    cleaned = 0
    for file in data_dir.glob("*.json"):
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})\.json", file.name)
        if not match:
            continue
        y, mo, d, h, mi, s = match.groups()
        file_ts = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=timezone.utc).timestamp()
        if file_ts < cutoff:
            file.unlink()
            cleaned += 1
    return cleaned


async def main() -> None:
    scan_start = time.time()
    cfg = {**get_config(), **_load_local_config()}
    proxy_url = _get_proxy_url(cfg)
    data_dir = _get_data_dir(cfg)
    max_concurrent = int(cfg.get("max_concurrent_requests", 10))
    retention_days = int(cfg.get("data_retention_days", 7))

    bj_tz = timezone(timedelta(hours=8))
    scan_time = datetime.now(bj_tz).strftime("%Y-%m-%d %H:%M:%S")
    log.info("========== SCAN START: %s ==========", scan_time)

    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
    async with aiohttp.ClientSession(headers=headers) as session:
        symbols = await fetch_all_symbols(session, proxy_url)
        if not symbols:
            sys.exit(1)
        all_sym = await fetch_all_data(session, symbols, proxy_url, max_concurrent)
        log.info("获取历史资金费率...")
        fund_rates = await fetch_history_fund_rates(session, symbols, proxy_url)

    log.info("计算技术指标...")
    compute_indicators(all_sym)

    try:
        market_caps = get_market_cap_map(
            ttl_seconds=int(cfg.get("auto_trade", {}).get("market_cap_cache_ttl_seconds", 86400)),
            required_symbols=list(all_sym.keys()),
            proxy_url=proxy_url,
            api_key=cfg.get("coingecko_demo_api_key") or None,
        )
        market_cap_hits = sum(
            1 for item in market_caps.values()
            if item.get("market_cap") is not None
        )
        log.info("CoinGecko 市值缓存可用: %d 个 symbol 有市值", market_cap_hits)
    except Exception as exc:
        market_caps = {}
        log.warning("CoinGecko 市值数据不可用，自动交易标签本轮不可用: %s", exc)

    leading = set(find_leading_coins(all_sym))
    result_tokens = []
    valid_count = 0
    anomaly_dict = {"15m": [], "1H": [], "4H": []}
    old_data_symbols: dict = {"15m": [], "1H": [], "4H": [], "1D": []}
    new_symbols: list[str] = []
    no_data_symbols: list[str] = []
    banned_symbols: list[str] = []

    for key, sym in all_sym.items():
        if is_banned_symbol(key, cfg):
            banned_symbols.append(key)
            continue
        if key == "BTCUSDT":
            continue
        if is_too_new(sym):
            new_symbols.append(key)
            continue
        if has_no_data(sym):
            no_data_symbols.append(key)
            continue
        if not is_data_fresh(sym, key, old_data_symbols):
            continue
        if not has_required_indicators(sym):
            continue

        market_cap_info = get_symbol_market_cap(key, market_caps)
        valid_count += 1
        total_fund_rate = fund_rates.get(key, 0.0)
        # 与实时自动交易共用同一套标签组装逻辑（core/tagging.py），避免漂移
        tags = build_symbol_tags(
            all_sym, key, sym, cfg,
            market_cap_info=market_cap_info,
            fund_rate=0.0,
            leading=leading,
            anomaly_dict=anomaly_dict,
        )

        # 市值仅在通过完整自动交易信号时展示，行为同旧逻辑但不再依赖展示标签。
        has_auto = evaluate_auto_trade_signal(
            key,
            sym,
            market_cap_info,
            max_market_cap=float(cfg.get("auto_trade", {}).get("market_cap_max", 1_000_000_000)),
            min_quote_volume=float(cfg.get("auto_trade", {}).get("min_quote_volume_1d", 500_000)),
            atr_min=float(cfg.get("auto_trade", {}).get("atr_min", 0.001)),
            atr_stop_multi=float(cfg.get("auto_trade", {}).get("atr_stop_multi", 1.2)),
        ) is not None
        last_bar = sym["1D"]["data"][-1]
        close = float(last_bar[4])
        open_price = float(last_bar[1])
        atr_series = sym.get("1D", {}).get("atr") or []
        atr_val = None
        if atr_series:
            try:
                last_atr = float(atr_series[-1])
                if math.isfinite(last_atr):
                    atr_val = round(last_atr, 8)
            except (TypeError, ValueError):
                atr_val = None
        result_tokens.append({
            "symbol": key,
            "price": close,
            "high_24h": float(last_bar[2]),
            "low_24h": float(last_bar[3]),
            "change_pct": round(((close - open_price) / open_price * 100) if open_price else 0, 2),
            "fund_rate": round(total_fund_rate, 6),
            "atr": atr_val,
            "market_cap": round(float(market_cap_info.get("market_cap") or 0), 2) if (has_auto and market_cap_info) else None,
            "market_cap_source": market_cap_info if has_auto else None,
            "tags": tags,
        })

    candidate_state = SimpleNamespace(buy_list={token["symbol"]: {} for token in result_tokens})
    select_by_volume(all_sym, candidate_state)
    fairy = set(find_fairy_guide(all_sym, candidate_state))
    for token in result_tokens:
        if token["symbol"] in fairy:
            token["tags"].append("仙人指路")

    max_positions = cfg.get("max_long_positions", 5)
    shortlist_n = max(3 * max_positions, 12)
    threshold = cfg.get("negative_funding_threshold", -0.05)
    shortlist = sorted(
        result_tokens,
        key=lambda token: len(token["tags"]),
        reverse=True,
    )[:shortlist_n]
    for token in shortlist:
        total_fund_rate = fund_rates.get(token["symbol"], 0.0)
        if total_fund_rate < threshold:
            token["tags"].append(f"负费率({total_fund_rate * 100:.2f}%)")

    result_tokens = [token for token in result_tokens if token["tags"]]
    log.info(
        "Pages 扫描过滤: ban=%d 新币=%d 空数据=%d 数据旧=%s",
        len(banned_symbols), len(new_symbols), len(no_data_symbols), old_data_symbols,
    )

    result_tokens.sort(
        key=lambda item: len(item["tags"]),
        reverse=True,
    )
    default_count = len(result_tokens)
    elapsed = round(time.time() - scan_start, 1)

    result = {
        "scanTime": scan_time,
        "totalSymbols": len(symbols),
        "validSymbols": valid_count,
        "filteredCount": default_count,
        "totalTagged": len(result_tokens),
        "elapsed": elapsed,
        "tokens": result_tokens,
    }

    data_dir.mkdir(parents=True, exist_ok=True)
    scan_id = datetime.now(bj_tz).strftime("%Y-%m-%dT%H-%M-%S")
    scan_file = data_dir / f"{scan_id}.json"
    scan_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("写入 %s", scan_file)

    cleaned = cleanup_old_scans(data_dir, retention_days)
    if cleaned:
        log.info("清理 %d 个旧数据文件", cleaned)
    log.info(
        "完成: %d个交易对, %d个可分析, %d个有标签, 默认标签%d个, 耗时%ss",
        len(symbols), valid_count, len(result_tokens), default_count, elapsed,
    )


if __name__ == "__main__":
    asyncio.run(main())
