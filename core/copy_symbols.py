"""Helpers for Bitget copy-trading symbol filters."""
from __future__ import annotations

from infra.logger import log


def parse_copy_symbols(resp: dict) -> set[str]:
    data = resp.get("data") or []
    if isinstance(data, dict):
        for key in ("symbolList", "symbols", "list"):
            if isinstance(data.get(key), list):
                data = data[key]
                break

    symbols: set[str] = set()
    for item in data:
        if isinstance(item, str):
            symbols.add(item)
        elif isinstance(item, dict):
            symbol = item.get("symbol") or item.get("symbolName")
            if symbol:
                symbols.add(str(symbol))
    return symbols


def get_copy_trading_symbols(ex) -> set[str]:
    resp = ex.copy_get_symbols(ex.PRODUCT_TYPE)
    if resp.get("code") != "00000":
        raise RuntimeError(resp.get("msg") or str(resp))
    symbols = parse_copy_symbols(resp)
    if not symbols:
        raise RuntimeError("Bitget 带单交易对列表为空")
    log.info("获取到 %d 个 Bitget 带单可开交易对", len(symbols))
    return symbols
