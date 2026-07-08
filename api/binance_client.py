"""
Binance 交易所适配器：实现 ExchangeAPI 接口

使用 Binance USDT-M Futures API (fapi)。
返回数据格式统一为与 Bitget 兼容的结构，
使上层业务代码无需关心交易所差异。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import requests

from api.exchange import ExchangeAPI
from api.retry import retry
from infra.env import NEED_PROXY, PROXIES
from infra.logger import log
from infra.util import get_time_ms


# Binance 周期映射：项目内部周期名 → Binance interval
_GRANULARITY_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m",
    "1H": "1h", "4H": "4h",
    "1D": "1d", "1W": "1w", "1M": "1M",
}


class BinanceClient(ExchangeAPI):
    HOST = "fapi.binance.com"
    PRODUCT_TYPE = "USDT-FUTURES"

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._session = requests.Session()
        self._session.headers.update({
            "X-MBX-APIKEY": api_key,
            "Content-Type": "application/json",
        })
        if NEED_PROXY:
            self._session.proxies.update(PROXIES)

    # ---- 签名 ----

    def _sign_params(self, params: dict) -> dict:
        """为请求参数添加 timestamp + signature"""
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        sig = hmac.new(
            self._api_secret.encode(), query.encode(), hashlib.sha256,
        ).hexdigest()
        params["signature"] = sig
        return params

    # ---- HTTP ----

    @retry(max_attempts=3, delay=2.0)
    def _get(self, path: str, params: dict | None = None) -> Any:
        params = self._sign_params(params or {})
        url = f"https://{self.HOST}{path}"
        resp = self._session.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    @retry(max_attempts=3, delay=2.0)
    def _post(self, path: str, params: dict | None = None) -> Any:
        params = self._sign_params(params or {})
        log.debug("host:%s method:%s data:%s", self.HOST, path, params)
        url = f"https://{self.HOST}{path}"
        resp = self._session.post(url, params=params)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    @retry(max_attempts=5, delay=2.0)
    def _public_get(url: str) -> requests.Response:
        if NEED_PROXY:
            return requests.get(url, proxies=PROXIES)
        return requests.get(url)

    # ---- 格式转换辅助 ----

    @staticmethod
    def _to_symbol(symbol: str) -> str:
        """项目内部 symbol (如 BTCUSDT) 直接兼容 Binance"""
        return symbol

    @staticmethod
    def _side_map(side: str, trade_side: str) -> dict:
        """
        将 Bitget 风格的 side+tradeSide 映射为 Binance side+positionSide
        Bitget: side=buy/sell, tradeSide=open/close
        Binance: side=BUY/SELL, positionSide=LONG/SHORT (双向持仓模式)
        """
        if trade_side == "open":
            if side == "buy":
                return {"side": "BUY", "positionSide": "LONG"}
            return {"side": "SELL", "positionSide": "SHORT"}
        else:  # close，调用方约定 side=持仓方向
            if side == "buy":
                # 平多：卖出 LONG 持仓
                return {"side": "SELL", "positionSide": "LONG"}
            # 平空：买入 SHORT 持仓
            return {"side": "BUY", "positionSide": "SHORT"}

    # ---- 账户 ----

    def get_accounts(self, product_type: str) -> dict:
        result = self._get("/fapi/v2/account")
        return {"data": [{"accountEquity": str(result["totalWalletBalance"])}]}

    def set_leverage(self, symbol, product_type, margin_coin,
                     leverage=None, long_leverage=None,
                     short_leverage=None, hold_side=None) -> dict:
        lev = leverage or long_leverage or short_leverage or 10
        result = self._post("/fapi/v1/leverage", {
            "symbol": self._to_symbol(symbol), "leverage": int(lev),
        })
        return {"data": result}

    def open_count(self, symbol, product_type, margin_coin,
                   open_amount, open_price, leverage) -> dict:
        """
        Binance 没有直接的 open-count 接口，
        根据可用余额和杠杆计算可开数量。
        """
        account = self._get("/fapi/v2/account")
        available = float(account["availableBalance"])
        price = float(open_price)
        lev = int(leverage)
        size = (available * lev) / price if price > 0 else 0
        return {"data": {"size": str(size)}}

    def set_position_margin(self, symbol, product_type, margin_coin,
                            amount, hold_side="long") -> dict:
        pos_side = "LONG" if hold_side == "long" else "SHORT"
        result = self._post("/fapi/v1/positionMargin", {
            "symbol": self._to_symbol(symbol),
            "positionSide": pos_side,
            "amount": str(amount),
            "type": 1,
        })
        return {"data": result}

    # ---- 订单 ----

    def live_order(self, symbol, product_type, margin_mode, margin_coin,
                   side, size, order_type, trade_side, price="",
                   preset_stop_loss="", preset_take_profit="") -> dict:
        side_info = self._side_map(side, trade_side)
        params = {
            "symbol": self._to_symbol(symbol),
            "side": side_info["side"],
            "positionSide": side_info["positionSide"],
            "type": "MARKET" if order_type == "market" else "LIMIT",
            "quantity": str(size),
        }
        if order_type == "limit" and price:
            params["price"] = str(price)
            params["timeInForce"] = "GTC"

        result = self._post("/fapi/v1/order", params)

        # 平仓方向与持仓方向相反
        close_side = "SELL" if side_info["positionSide"] == "LONG" else "BUY"

        # 如果有预设止损，额外下一个止损单
        if preset_stop_loss:
            self._post("/fapi/v1/order", {
                "symbol": self._to_symbol(symbol),
                "side": close_side,
                "positionSide": side_info["positionSide"],
                "type": "STOP_MARKET",
                "stopPrice": str(preset_stop_loss),
                "closePosition": "true",
            })

        # 如果有预设止盈，额外下一个止盈单
        if preset_take_profit:
            self._post("/fapi/v1/order", {
                "symbol": self._to_symbol(symbol),
                "side": close_side,
                "positionSide": side_info["positionSide"],
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": str(preset_take_profit),
                "closePosition": "true",
            })

        return {"data": {"orderId": str(result["orderId"])}}

    def get_order_detail(self, symbol, product_type, order_id) -> dict:
        result = self._get("/fapi/v1/order", {
            "symbol": self._to_symbol(symbol),
            "orderId": int(order_id),
        })
        # 转换为统一格式
        return {"data": {
            "state": "filled" if result["status"] == "FILLED" else result["status"].lower(),
            "orderId": str(result["orderId"]),
            "priceAvg": result.get("avgPrice", result.get("price", "0")),
            "baseVolume": result.get("executedQty", "0"),
            "quoteVolume": str(
                float(result.get("executedQty", 0))
                * float(result.get("avgPrice", result.get("price", 0)))
            ),
            "fee": "0",  # Binance 不在订单详情中返回手续费
            "totalProfits": str(result.get("realizedPnl", "0")),
            "cTime": str(result.get("time", "")),
            "tradeSide": result.get("side", "").lower(),
        }}

    def get_orders_pending(self, product_type) -> dict:
        result = self._get("/fapi/v1/openOrders")
        return {"data": result}

    # ---- 持仓 ----

    def get_all_position(self, product_type) -> dict:
        result = self._get("/fapi/v2/positionRisk")
        # 只返回有持仓量的
        positions = []
        for p in result:
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            positions.append({
                "symbol": p["symbol"],
                "holdSide": "long" if amt > 0 else "short",
                "available": str(abs(amt)),
                "total": str(abs(amt)),
                "openPriceAvg": p.get("entryPrice", "0"),
                "cTime": str(int(float(p.get("updateTime", 0)))),
                "unrealizedPL": p.get("unRealizedProfit", "0"),
                "marginMode": p.get("marginType", "isolated").lower(),
                "leverage": p.get("leverage", "10"),
            })
        return {"data": positions}

    def get_history_position(self, product_type, start_time) -> dict:
        """
        Binance 没有直接的历史仓位接口，
        通过交易历史模拟：获取最近的成交记录。
        """
        result = self._get("/fapi/v1/userTrades", {
            "startTime": int(start_time), "limit": 500,
        })
        # 聚合为仓位级别的盈亏
        pnl_by_symbol: dict[str, float] = {}
        for t in result:
            sym = t["symbol"]
            pnl_by_symbol[sym] = pnl_by_symbol.get(sym, 0) + float(
                t.get("realizedPnl", 0))
        position_list = [
            {"symbol": sym, "netProfit": str(pnl)}
            for sym, pnl in pnl_by_symbol.items()
        ]
        return {"data": {"list": position_list}}

    def get_fill_history(self, product_type, start_time) -> dict:
        result = self._get("/fapi/v1/userTrades", {
            "startTime": int(start_time), "limit": 500,
        })
        fill_list = []
        for t in result:
            # Binance 没有 burst_close 概念，通过 ADL 标记判断
            trade_side = t.get("side", "").lower()
            if t.get("maker", False) is False and float(
                    t.get("realizedPnl", 0)) < -50:
                # 大额亏损平仓近似为爆仓
                trade_side = (
                    "burst_close_long" if t["side"] == "SELL"
                    else "burst_close_short"
                )
            fill_list.append({
                "symbol": t["symbol"],
                "tradeSide": trade_side,
                "price": t.get("price", "0"),
                "qty": t.get("qty", "0"),
                "realizedPnl": t.get("realizedPnl", "0"),
                "time": str(t.get("time", "")),
            })
        return {"data": {"fillList": fill_list}}

    # ---- 行情 ----

    def get_all_symbol(self, product_type) -> dict:
        url = f"https://{self.HOST}/fapi/v1/ticker/24hr"
        resp = self._public_get(url)
        data = resp.json()
        return {"data": [{"symbol": t["symbol"]} for t in data
                         if t["symbol"].endswith("USDT")]}

    def get_klines_url(self, symbol, product_type, granularity,
                       limit="100", end_time="") -> str:
        interval = _GRANULARITY_MAP.get(granularity, granularity.lower())
        q = f"symbol={self._to_symbol(symbol)}&interval={interval}&limit={limit}"
        if end_time:
            q += f"&endTime={end_time}"
        return f"https://{self.HOST}/fapi/v1/klines?{q}"

    def get_history_fund_rate(self, symbol, product_type,
                              page_size="20") -> dict:
        url = (f"https://{self.HOST}/fapi/v1/fundingRate"
               f"?symbol={self._to_symbol(symbol)}&limit={page_size}")
        resp = self._public_get(url)
        data = resp.json()
        return {"data": [
            {"fundingRate": str(r.get("fundingRate", "0")),
             "fundingTime": str(r.get("fundingTime", ""))}
            for r in data
        ]}

    def get_contracts(self, symbol, product_type) -> dict:
        url = f"https://{self.HOST}/fapi/v1/exchangeInfo"
        resp = self._public_get(url)
        info = resp.json()
        for s in info.get("symbols", []):
            if s["symbol"] == self._to_symbol(symbol):
                price_precision = s.get("pricePrecision", 2)
                qty_precision = s.get("quantityPrecision", 3)
                min_qty = "0.001"
                for flt in s.get("filters", []):
                    if flt.get("filterType") == "LOT_SIZE":
                        min_qty = str(flt.get("minQty", min_qty))
                        break
                return {"data": [{
                    "pricePlace": str(price_precision),
                    "volumePlace": str(qty_precision),
                    "minTradeNum": min_qty,
                    "symbol": s["symbol"],
                }]}
        return {"data": [{"pricePlace": "2", "volumePlace": "3",
                          "minTradeNum": "0.001"}]}
