"""
Bitget API 封装：签名、请求、交易接口

所有与 Bitget 交易所的 HTTP 交互都在此模块中完成。
使用 requests.Session 复用连接，统一异常处理。
"""
from __future__ import annotations

import base64
import hmac
import json
import time
from typing import Any

import requests

from infra.env import NEED_PROXY, PROXIES, API_KEY, API_SECRET, API_PASSPHRASE, BITGET_DEMO
from infra.logger import log
from api.retry import retry
from infra.util import get_time_ms

# ---- 常量 ----
HOST = "api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"

# 复用连接的 Session
_session = requests.Session()
_headers = {
    "ACCESS-KEY": API_KEY,
    "ACCESS-PASSPHRASE": API_PASSPHRASE,
    "locale": "zh-CN",
    "Content-Type": "application/json",
}
if BITGET_DEMO:
    _headers["paptrading"] = "1"
_session.headers.update(_headers)
if NEED_PROXY:
    _session.proxies.update(PROXIES)


# =============================================================================
#  签名与请求
# =============================================================================

def _hmac_sha256_base64(message: str, secret: str) -> str:
    """HMAC-SHA256 签名并 Base64 编码"""
    mac = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        digestmod="sha256",
    )
    return base64.b64encode(mac.digest()).decode("utf-8")


def _sign_headers(method: str, path: str, query: str = "", body: str = "") -> dict:
    """构造带签名的请求头"""
    ts = get_time_ms()
    sign_str = ts + method + path + query + body
    return {
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-SIGN": _hmac_sha256_base64(sign_str, API_SECRET),
    }


@retry(max_attempts=3, delay=2.0)
def _get(path: str, query_str: str) -> Any:
    """带签名的 GET 请求"""
    headers = _sign_headers("GET", path, "?" + query_str)
    url = f"https://{HOST}{path}?{query_str}"
    resp = _session.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


@retry(max_attempts=3, delay=2.0)
def _post(path: str, data: dict) -> Any:
    """带签名的 POST 请求（修复：仅使用 json 参数，不再同时传 data）"""
    body = json.dumps(data)
    log.debug("host:%s method:%s data:%s", HOST, path, body)
    headers = _sign_headers("POST", path, "", body)
    url = f"https://{HOST}{path}"
    resp = _session.post(url, json=data, headers=headers)
    resp.raise_for_status()
    return resp.json()


@retry(max_attempts=5, delay=2.0)
def _simple_get(url: str) -> requests.Response:
    """简单 GET 请求"""
    if NEED_PROXY:
        return requests.get(url, proxies=PROXIES)
    return requests.get(url)


# =============================================================================
#  交易接口
# =============================================================================

def setLeverage(symbol: str, product_type: str, margin_coin: str,
                leverage=None, long_leverage=None, short_leverage=None,
                hold_side=None) -> dict:
    """调整合约杠杆倍数"""
    data: dict = {"symbol": symbol, "marginCoin": margin_coin, "productType": product_type}
    if leverage is not None:
        data["leverage"] = leverage
    if long_leverage is not None:
        data["longLeverage"] = long_leverage
    if short_leverage is not None:
        data["shortLeverage"] = short_leverage
    if hold_side is not None:
        data["holdSide"] = hold_side
    return _post("/api/v2/mix/account/set-leverage", data)


def getOrderDetail(symbol: str, product_type: str, order_id: str) -> dict:
    """获取订单详情"""
    q = f"orderId={order_id}&productType={product_type}&symbol={symbol}"
    return _get("/api/v2/mix/order/detail", q)


def getOrdersPending(product_type: str) -> dict:
    """获取挂单列表"""
    return _get("/api/v2/mix/order/orders-pending", f"productType={product_type}")


def liveOrder(symbol: str, product_type: str, margin_mode: str,
              margin_coin: str, side: str, size, order_type: str,
              trade_side: str, price: str = "", preset_stop_loss: str = "") -> dict:
    """下单（市价/限价）"""
    data = {
        "symbol": symbol, "marginCoin": margin_coin, "size": size,
        "side": side, "orderType": order_type, "productType": product_type,
        "marginMode": margin_mode, "tradeSide": trade_side,
    }
    if order_type == "limit":
        data["price"] = str(price)
    if preset_stop_loss:
        data["presetStopLossPrice"] = preset_stop_loss
    return _post("/api/v2/mix/order/place-order", data)


def getAllPosition(product_type: str) -> dict:
    """获取所有持仓"""
    resp = _get("/api/v2/mix/position/all-position", f"productType={product_type}")
    if resp.get("data") is None:
        return {"data": []}
    return resp


def getHistoryPosition(product_type: str, start_time: str) -> dict:
    """获取历史仓位"""
    q = f"productType={product_type}&startTime={start_time}"
    return _get("/api/v2/mix/position/history-position", q)


def getFillHistory(product_type: str, start_time) -> dict:
    """获取成交历史"""
    q = f"productType={product_type}&startTime={start_time}"
    return _get("/api/v2/mix/order/fill-history", q)


def openCount(symbol: str, product_type: str, margin_coin: str,
              open_amount: str, open_price: str, leverage: str) -> dict:
    """查询可开数量"""
    q = (f"productType={product_type}&symbol={symbol}&marginCoin={margin_coin}"
         f"&openAmount={open_amount}&openPrice={open_price}&leverage={leverage}")
    return _get("/api/v2/mix/account/open-count", q)


def getAccounts(product_type: str) -> dict:
    """获取账户信息"""
    return _get("/api/v2/mix/account/accounts", f"productType={product_type}")


def getAllSymbol(host_: str, product_type: str) -> dict:
    """获取所有 USDT 合约交易对"""
    url = f"https://{host_}/api/v2/mix/market/tickers?productType={product_type}"
    resp = _simple_get(url)
    return resp.json()


def getKlinesURL(symbol: str, product_type: str, granularity: str,
                 limit: str = "100", end_time: str = "") -> str:
    """构造 K 线请求 URL"""
    q = f"symbol={symbol}&productType={product_type}&granularity={granularity}&limit={limit}"
    if end_time:
        q += f"&endTime={end_time}"
    return f"https://{HOST}/api/v2/mix/market/candles?{q}"


def getHistoryFundRate(symbol: str, product_type: str, page_size: str = "20") -> dict:
    """获取历史资金费率"""
    q = f"symbol={symbol}&productType={product_type}&pageSize={page_size}"
    resp = _simple_get(f"https://{HOST}/api/v2/mix/market/history-fund-rate?{q}")
    return resp.json()


def getContracts(symbol: str, product_type: str) -> dict:
    """获取合约信息（价格精度、最小开单量等）"""
    q = f"symbol={symbol}&productType={product_type}"
    resp = _simple_get(f"https://{HOST}/api/v2/mix/market/contracts?{q}")
    return resp.json()
