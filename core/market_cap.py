"""CoinGecko market-cap cache used by the trading strategy."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

from infra.env import NEED_PROXY, PROXIES
from infra.logger import log

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "runtime" / "cache"
MARKET_CAP_CACHE = CACHE_DIR / "coingecko_market_caps.json"
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"


def _read_cache(path: Path = MARKET_CAP_CACHE) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("读取市值缓存失败: %s", exc)
        return {}


def _write_cache(payload: dict[str, Any], path: Path = MARKET_CAP_CACHE) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _symbol_from_exchange(symbol: str) -> str:
    for suffix in ("USDT", "USDC", "FDUSD", "USD"):
        if symbol.upper().endswith(suffix):
            return symbol[:-len(suffix)].lower()
    return symbol.lower()


def _fresh_symbols(payload: dict[str, Any], ttl_seconds: int) -> dict[str, dict[str, Any]]:
    now = time.time()
    result: dict[str, dict[str, Any]] = {}
    for symbol, info in (payload.get("symbols") or {}).items():
        updated_at = float(info.get("updated_at", payload.get("updated_at", 0)) or 0)
        if now - updated_at < ttl_seconds:
            result[symbol] = info
    return result


def get_market_cap_map(
    ttl_seconds: int = 86400,
    force_refresh: bool = False,
    required_symbols: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return a map keyed by lowercase token symbol.

    CoinGecko symbols are not globally unique. We keep the highest market-cap
    coin for each symbol and record enough metadata for later audit logs.
    """
    cached = _read_cache()
    if not force_refresh:
        fresh = _fresh_symbols(cached, ttl_seconds)
        required = {_symbol_from_exchange(symbol) for symbol in (required_symbols or [])}
        if fresh and (not required or required.issubset(fresh.keys())):
            return fresh

    symbols: dict[str, dict[str, Any]] = {}
    updated_at = time.time()
    page = 1
    min_market_cap_seen = None
    proxies = PROXIES if NEED_PROXY else None
    while True:
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": page,
            "sparkline": "false",
        }
        resp = requests.get(COINGECKO_MARKETS_URL, params=params, proxies=proxies, timeout=20)
        if resp.status_code == 429:
            raise RuntimeError("CoinGecko 限流，无法刷新市值缓存")
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break

        for row in rows:
            market_cap = row.get("market_cap")
            symbol = str(row.get("symbol") or "").lower()
            if not symbol or market_cap is None:
                continue
            market_cap = float(market_cap)
            old = symbols.get(symbol)
            if old is None or market_cap > float(old.get("market_cap", 0)):
                symbols[symbol] = {
                    "id": row.get("id"),
                    "symbol": symbol,
                    "name": row.get("name"),
                    "market_cap": market_cap,
                    "current_price": row.get("current_price"),
                    "matched_by": "coingecko_symbol_highest_market_cap",
                    "updated_at": updated_at,
                }
            min_market_cap_seen = market_cap

        if min_market_cap_seen is not None and min_market_cap_seen < 5_000_000:
            break
        page += 1

    for required in required_symbols or []:
        token_symbol = _symbol_from_exchange(required)
        symbols.setdefault(token_symbol, {
            "id": None,
            "symbol": token_symbol,
            "name": None,
            "market_cap": None,
            "current_price": None,
            "matched_by": "coingecko_missing_or_below_threshold",
            "updated_at": updated_at,
        })

    payload = {"updated_at": updated_at, "symbols": symbols}
    _write_cache(payload)
    log.info("CoinGecko 市值缓存已更新: %d 个 symbol", len(symbols))
    return symbols


def get_symbol_market_cap(symbol: str, market_caps: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    return market_caps.get(_symbol_from_exchange(symbol))
