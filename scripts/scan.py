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
CYCLES = ["1D", "4H", "1H", "15m"]

sys.path.insert(0, str(ROOT))

from core.data_fetcher import compute_indicators  # noqa: E402
from core.scanner import (  # noqa: E402
    detect_consolidation_breakout,
    detect_early_strong_trend,
    detect_volume_anomaly,
    find_fairy_guide,
    find_leading_coins,
    select_by_volume,
)
from core.strategy import (  # noqa: E402
    is_15m_trend_up,
    is_1d_boll_trend_up,
    is_1d_trend_up,
    is_1h_trend_up,
    is_4h_trend_up,
    is_btc_12h_not_down,
    is_btc_trend_down,
    is_btc_trend_up,
)

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


async def dispatch_crypto_mint_analysis(
    session: aiohttp.ClientSession,
    symbols: list[str],
    cfg: dict[str, Any],
) -> bool:
    tokens = [symbol_to_news_token(symbol) for symbol in symbols]
    tokens = [token for token in dict.fromkeys(tokens) if token]
    if not tokens:
        return False

    token = (
        os.environ.get("CRYPTO_MINT_GITHUB_TOKEN")
        or cfg.get("crypto_mint_github_token")
        or cfg.get("github_token")
    )
    if not token:
        log.info("未配置 CRYPTO_MINT_GITHUB_TOKEN，跳过自动触发消息面分析")
        return False

    payload = {
        "ref": cfg.get("crypto_mint_branch", "main"),
        "inputs": {
            "token": " ".join(tokens),
            "exchange": cfg.get("crypto_mint_exchange", "binance"),
        },
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
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
                log.info("已批量触发 Crypto Mint 分析: %s", " ".join(tokens))
                return True
            text = await resp.text()
            log.warning("触发 Crypto Mint 失败 HTTP %d: %s", resp.status, text[:200])
    except Exception as exc:
        log.warning("触发 Crypto Mint 异常: %s", exc)
    return False


async def fetch_crypto_mint_sentiment(
    session: aiohttp.ClientSession,
    symbols: list[str],
    proxy_url: str | None,
) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}

    tokens_by_symbol = {symbol: symbol_to_news_token(symbol) for symbol in symbols}
    index_url = f"{CRYPTO_MINT_BASE}data/search-index.json"
    index = await fetch_json(session, f"{index_url}?t={int(time.time())}", proxy_url)
    indexed: dict[str, dict[str, Any]] = {}
    for item in (index or {}).get("results", []):
        token = str(item.get("token") or "").upper()
        if token:
            indexed[token] = item

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


def is_not_rubbish(sym: dict) -> bool:
    try:
        for i in range(-3, 0):
            if float(sym["1D"]["data"][i][2]) > float(sym["1D"]["data"][i][3]) * 1.1:
                return True
    except (IndexError, KeyError, ValueError):
        return False
    return False


def min_price_7d(sym: dict) -> float:
    data = sym["1D"]["data"]
    days = min(7, len(data))
    return min(float(data[-i][3]) for i in range(1, days + 1))


def check_anti_chase(sym: dict, cfg: dict[str, Any]) -> bool:
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


def is_trend_confluence(sym: dict) -> bool:
    return (
        is_15m_trend_up(sym, "15m")
        and is_1h_trend_up(sym, "1H")
        and is_4h_trend_up(sym, "4H")
        and is_1d_trend_up(sym)
    )


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
    cfg = _load_local_config()
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

    btc_up = is_btc_trend_up(all_sym)
    btc_down = is_btc_trend_down(all_sym)
    btc_12h_ok = is_btc_12h_not_down(all_sym)
    btc_direction = "up" if btc_up else ("down" if btc_down else "neutral")
    log.info("BTC 方向: %s, 12h_not_down=%s", btc_direction, btc_12h_ok)

    leading = set(find_leading_coins(all_sym))
    result_tokens = []
    valid_count = 0
    anomaly_dict = {"15m": [], "1H": [], "4H": []}

    for key, sym in all_sym.items():
        if key == "BTCUSDT" or not is_valid_symbol(sym):
            continue
        valid_count += 1
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

        anomaly_tf = detect_volume_anomaly(all_sym, key, "buy", anomaly_dict)
        if anomaly_tf:
            tags.append(f"成交量异动({anomaly_tf})")
        if btc_up:
            tags.append("BTC看多")
        if btc_12h_ok:
            tags.append("BTC近12h未跌")
        if check_anti_chase(sym, cfg):
            tags.append("未追高")

        total_fund_rate = fund_rates.get(key, 0.0)
        if total_fund_rate < cfg.get("negative_funding_threshold", -0.05):
            tags.append(f"负费率({total_fund_rate * 100:.2f}%)")
        if is_not_rubbish(sym):
            tags.append("波动充足")
        if key in leading:
            tags.append("龙头币")
        if detect_consolidation_breakout(sym, "1H"):
            tags.append("盘整突破")
        if detect_early_strong_trend(sym):
            tags.append("强势启动")

        if not tags:
            continue

        last_bar = sym["1D"]["data"][-1]
        close = float(last_bar[4])
        open_price = float(last_bar[1])
        result_tokens.append({
            "symbol": key,
            "price": close,
            "high_24h": float(last_bar[2]),
            "low_24h": float(last_bar[3]),
            "change_pct": round(((close - open_price) / open_price * 100) if open_price else 0, 2),
            "fund_rate": round(total_fund_rate, 6),
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
    async with aiohttp.ClientSession(headers=headers) as session:
        if cfg.get("crypto_mint_auto_dispatch", False):
            await dispatch_crypto_mint_analysis(session, daily_up_symbols, cfg)
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
        "totalTagged": len(result_tokens),
        "btcDirection": btc_direction,
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
