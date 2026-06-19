"""
交易所工厂：根据环境变量创建对应的交易所客户端

使用方式：
    from api.factory import get_exchange
    exchange = get_exchange()
    exchange.get_accounts(exchange.PRODUCT_TYPE)
"""
from __future__ import annotations

from api.exchange import ExchangeAPI
from infra.env import (
    EXCHANGE, API_KEY, API_SECRET, API_PASSPHRASE,
    BINANCE_API_KEY, BINANCE_API_SECRET,
)

_instance: ExchangeAPI | None = None


def get_exchange() -> ExchangeAPI:
    """获取交易所单例"""
    global _instance
    if _instance is not None:
        return _instance

    if EXCHANGE == "binance":
        from api.binance_client import BinanceClient
        _instance = BinanceClient(BINANCE_API_KEY, BINANCE_API_SECRET)
    else:
        from api.bitget_client import BitgetClient
        _instance = BitgetClient(API_KEY, API_SECRET, API_PASSPHRASE)

    return _instance


def reset_exchange() -> None:
    """重置单例（测试用）"""
    global _instance
    _instance = None
