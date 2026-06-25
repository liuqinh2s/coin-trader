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
CRYPTO_MINT_BASE = "https://liuqinh2s.github.io/crypto-mint/"
CRYPTO_MINT_WORKFLOW_URL = "https://api.github.com/repos/liuqinh2s/crypto-mint/actions/workflows/analyze-token.yml/dispatches"
PRODUCT_TYPE = "USDT-FUTURES"
CYCLES = ["1W", "1D", "4H", "1H", "15m"]

sys.path.insert(0, str(ROOT))

from core.data_fetcher import compute_indicators  # noqa: E402
from core.auto_strategy import evaluate_auto_trade_signal  # noqa: E402
from core.copy_symbols import parse_copy_symbols  # noqa: E402
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


def symbol_to_news_token(symbol: str) -> str:
    token = re.sub(r"(USDT|USDC|FDUSD|USD)$", "", symbol.upper())
    return token.lstrip("$")


def crypto_mint_token_url(token: str) -> str:
    return f"{CRYPTO_MINT_BASE}{token}"


def get_crypto_mint_github_token(cfg: dict[str, Any]) -> str:
    return (
        os.environ.get("CRYPTO_MINT_GITHUB_TOKEN")
        or cfg.get("crypto_mint_github_token")
        or cfg.get("github_token")
        or ""
    ).strip()


def should_auto_dispatch_crypto_mint(cfg: dict[str, Any]) -> bool:
    env_value = os.environ.get("CRYPTO_MINT_AUTO_DISPATCH")
    if env_value is not None:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    if "crypto_mint_auto_dispatch" in cfg:
        return bool(cfg.get("crypto_mint_auto_dispatch"))
    return bool(get_crypto_mint_github_token(cfg))


async def dispatch_crypto_mint_analysis(
    session: aiohttp.ClientSession,
    symbols: list[str],
    cfg: dict[str, Any],
) -> int:
    tokens = [symbol_to_news_token(symbol) for symbol in symbols]
    tokens = [token for token in dict.fromkeys(tokens) if token]
    if not tokens:
        return 0

    token = get_crypto_mint_github_token(cfg)
    if not token:
        raise RuntimeError("未配置 CRYPTO_MINT_GITHUB_TOKEN，无法触发 Crypto Mint 分析")

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token_input = ",".join(tokens)
    payload = {
        "ref": cfg.get("crypto_mint_branch", "main"),
        "inputs": {
            "token": token_input,
            "exchange": cfg.get("crypto_mint_exchange", "other"),
        },
    }

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with session.post(
            CRYPTO_MINT_WORKFLOW_URL,
            headers=headers,
            json=payload,
            timeout=timeout,
        ) as resp:
            if resp.status in (200, 201, 202, 204):
                log.info(
                    "已触发 Crypto Mint 分析: token=%s exchange=%s",
                    token_input, payload["inputs"]["exchange"],
                )
                return len(tokens)
            text = await resp.text()
            log.warning("触发 Crypto Mint 失败 HTTP %d: %s", resp.status, text[:200])
    except Exception as exc:
        log.warning("触发 Crypto Mint 异常: %s", exc)
    return 0


async def fetch_crypto_mint_index(
    session: aiohttp.ClientSession,
    proxy_url: str | None,
) -> dict[str, dict[str, Any]]:
    index_url = f"{CRYPTO_MINT_BASE}data/search-index.json"
    index = await fetch_json(session, f"{index_url}?t={int(time.time())}", proxy_url)
    indexed: dict[str, dict[str, Any]] = {}
    for item in (index or {}).get("results", []):
        token = str(item.get("token") or "").upper()
        if token:
            indexed[token] = item
    return indexed


async def wait_for_crypto_mint_results(
    session: aiohttp.ClientSession,
    symbols: list[str],
    proxy_url: str | None,
    timeout_seconds: int,
    interval_seconds: int,
) -> int:
    if not symbols or timeout_seconds <= 0:
        return 0

    pending = {symbol_to_news_token(symbol) for symbol in symbols}
    pending = {token for token in pending if token}
    if not pending:
        return 0

    deadline = time.time() + timeout_seconds
    found = 0
    while time.time() < deadline:
        await asyncio.sleep(interval_seconds)
        indexed = await fetch_crypto_mint_index(session, proxy_url)
        ready = pending.intersection(indexed.keys())
        if ready:
            found += len(ready)
            pending -= ready
            log.info("Crypto Mint 新增完成 %d 个，剩余 %d 个", len(ready), len(pending))
        if not pending:
            break
    return found


async def dispatch_missing_crypto_mint_analysis(
    session: aiohttp.ClientSession,
    symbols: list[str],
    proxy_url: str | None,
    cfg: dict[str, Any],
) -> tuple[int, list[str]]:
    indexed = await fetch_crypto_mint_index(session, proxy_url)
    missing = [
        symbol for symbol in symbols
        if symbol_to_news_token(symbol) not in indexed
    ]
    if not missing:
        log.info("Crypto Mint 已有全部日K向上代币的结果")
        return 0, []
    log.info("Crypto Mint 缺失 %d 个结果，将一次性提交", len(missing))
    dispatched = await dispatch_crypto_mint_analysis(session, missing, cfg)
    return dispatched, missing[:dispatched]


async def fetch_crypto_mint_sentiment(
    session: aiohttp.ClientSession,
    symbols: list[str],
    proxy_url: str | None,
) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}

    tokens_by_symbol = {symbol: symbol_to_news_token(symbol) for symbol in symbols}
    indexed = await fetch_crypto_mint_index(session, proxy_url)

    sem = asyncio.Semaphore(8)
    result: dict[str, dict[str, Any]] = {}

    async def _fetch_one(symbol: str, token: str) -> None:
        indexed_item = indexed.get(token, {})
        detail_path = indexed_item.get("latestPath") or f"data/results/{token}-latest.json"
        detail_url = f"{CRYPTO_MINT_BASE}{detail_path}"
        sentiment = {
            "token": token,
            "available": False,
            "score": None,
            "label": "",
            "action": "",
            "name": indexed_item.get("name", ""),
            "generated_at": indexed_item.get("generatedAt", ""),
            "detail_url": crypto_mint_token_url(token),
            "summary": "",
        }

        if not indexed_item:
            result[symbol] = sentiment
            return

        async with sem:
            data = await fetch_json(session, f"{detail_url}?t={int(time.time())}", proxy_url)

        analysis = (data or {}).get("analysis", {})
        rating = analysis.get("rating", {})
        recommendation = analysis.get("recommendation", {})
        score = rating.get("score", indexed_item.get("score"))
        try:
            score = float(score)
            if score.is_integer():
                score = int(score)
        except (TypeError, ValueError, AttributeError):
            score = None

        sentiment.update({
            "available": score is not None,
            "score": score,
            "label": rating.get("label") or indexed_item.get("label", ""),
            "action": recommendation.get("action") or indexed_item.get("action", ""),
            "name": analysis.get("name") or indexed_item.get("name", ""),
            "generated_at": (data or {}).get("generatedAt") or analysis.get("generatedAt") or indexed_item.get("generatedAt", ""),
            "summary": analysis.get("summary", ""),
        })
        result[symbol] = sentiment

    await asyncio.gather(*[
        _fetch_one(symbol, token)
        for symbol, token in tokens_by_symbol.items()
    ], return_exceptions=True)
    return result


async def fetch_all_symbols(session: aiohttp.ClientSession, proxy_url: str | None) -> list[str]:
    url = f"{BITGET_API}/api/v2/mix/market/tickers?productType={PRODUCT_TYPE}"
    data = await fetch_json(session, url, proxy_url)
    if not data or data.get("code") != "00000" or not data.get("data"):
        log.error("tickers 返回异常: %s", str(data)[:200] if data else "None")
        return []
    symbols = [item["symbol"] for item in data["data"]]
    log.info("获取到 %d 个 USDT 永续合约", len(symbols))
    return symbols


async def fetch_copy_trading_symbols(
    session: aiohttp.ClientSession,
    proxy_url: str | None,
) -> set[str]:
    url = (
        f"{BITGET_API}/api/v2/copy/mix-trader/config-query-symbols"
        f"?productType={PRODUCT_TYPE}"
    )
    data = await fetch_json(session, url, proxy_url)
    if not data or data.get("code") != "00000":
        log.warning("带单交易对列表返回异常: %s", str(data)[:200] if data else "None")
        return set()
    symbols = parse_copy_symbols(data)
    log.info("获取到 %d 个 Bitget 带单可开交易对", len(symbols))
    return symbols


async def fetch_klines(
    session: aiohttp.ClientSession,
    symbol: str,
    granularity: str,
    proxy_url: str | None,
    limit: int = 200,
) -> tuple[str, str, list]:
    url = (
        f"{BITGET_API}/api/v2/mix/market/candles"
        f"?symbol={symbol}&productType={PRODUCT_TYPE}"
        f"&granularity={granularity}&limit={limit}"
    )
    data = await fetch_json(session, url, proxy_url)
    if not data or data.get("code") != "00000" or not data.get("data"):
        return symbol, granularity, []
    return symbol, granularity, data["data"]


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


def is_valid_symbol(sym: dict) -> bool:
    for tf in ("1D", "4H", "1H", "15m"):
        if tf not in sym or not sym[tf].get("data") or len(sym[tf]["data"]) < 26:
            return False
        if "bolling" not in sym[tf] or "macd" not in sym[tf]:
            return False
    return len(sym["1D"]["data"]) >= 20


def default_selected(tags: list[str]) -> bool:
    base_tags = {tag.split("(")[0] for tag in tags}
    return "日K趋势向上" in base_tags


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
        copy_symbols = await fetch_copy_trading_symbols(session, proxy_url)
        if not copy_symbols:
            log.error("未获取到 Bitget 带单可开交易对，停止扫描")
            sys.exit(1)
        before = len(symbols)
        symbols = [symbol for symbol in symbols if symbol in copy_symbols]
        log.info("带单过滤：移除 %d 个不支持带单的交易对，剩余 %d 个",
                 before - len(symbols), len(symbols))
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

    for key, sym in all_sym.items():
        if key == "BTCUSDT" or not is_valid_symbol(sym):
            continue
        valid_count += 1

        market_cap_info = get_symbol_market_cap(key, market_caps)
        total_fund_rate = fund_rates.get(key, 0.0)
        # 与实时自动交易共用同一套标签组装逻辑（core/tagging.py），避免漂移
        tags = build_symbol_tags(
            all_sym, key, sym, cfg,
            market_cap_info=market_cap_info,
            fund_rate=total_fund_rate,
            leading=leading,
            anomaly_dict=anomaly_dict,
        )

        if not tags:
            continue

        # 市值仅在通过完整自动交易信号时展示，行为同旧逻辑但不再依赖展示标签。
        has_auto = evaluate_auto_trade_signal(
            key,
            sym,
            market_cap_info,
            min_market_cap=float(cfg.get("auto_trade", {}).get("market_cap_min", 5_000_000)),
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
    low_vol = set(select_by_volume(all_sym, candidate_state))
    fairy = set(find_fairy_guide(all_sym, candidate_state))
    for token in result_tokens:
        if token["symbol"] in low_vol:
            token["tags"].append("小量大涨")
        if token["symbol"] in fairy:
            token["tags"].append("仙人指路")

    daily_up_symbols = [
        token["symbol"] for token in result_tokens
        if default_selected(token["tags"])
    ]
    log.info("获取消息面评分: %d 个日K趋势向上代币", len(daily_up_symbols))
    sentiment_dispatched = 0
    async with aiohttp.ClientSession(headers=headers) as session:
        if should_auto_dispatch_crypto_mint(cfg):
            sentiment_dispatched, dispatched_symbols = await dispatch_missing_crypto_mint_analysis(
                session, daily_up_symbols, proxy_url, cfg,
            )
            if sentiment_dispatched:
                wait_seconds = int(cfg.get("crypto_mint_wait_seconds", 180))
                wait_interval = int(cfg.get("crypto_mint_wait_interval_seconds", 15))
                wait_interval = max(5, wait_interval)
                log.info("等待 Crypto Mint 生成结果，最多 %d 秒", wait_seconds)
                await wait_for_crypto_mint_results(
                    session, dispatched_symbols, proxy_url, wait_seconds, wait_interval,
                )
        else:
            log.info("未开启 Crypto Mint 自动提交，仅读取已发布评分")
        sentiment_map = await fetch_crypto_mint_sentiment(session, daily_up_symbols, proxy_url)

    sentiment_count = 0
    for token in result_tokens:
        sentiment = sentiment_map.get(token["symbol"])
        if sentiment:
            token["sentiment"] = sentiment
            if sentiment.get("available"):
                sentiment_count += 1

    result_tokens.sort(
        key=lambda item: (
            item.get("sentiment", {}).get("score")
            if item.get("sentiment", {}).get("score") is not None
            else -1,
            len(item["tags"]),
        ),
        reverse=True,
    )
    default_count = sum(1 for token in result_tokens if default_selected(token["tags"]))
    elapsed = round(time.time() - scan_start, 1)

    result = {
        "scanTime": scan_time,
        "totalSymbols": len(symbols),
        "validSymbols": valid_count,
        "filteredCount": default_count,
        "sentimentCount": sentiment_count,
        "sentimentDispatched": sentiment_dispatched,
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
