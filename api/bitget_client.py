"""
Bitget 交易所适配器：实现 ExchangeAPI 接口

将原有 bitget_api.py 的函数式接口封装为类，
保持签名逻辑和请求方式不变。
"""
from __future__ import annotations

import base64
import hmac
import json
from typing import Any

import requests

from api.exchange import ExchangeAPI
from api.retry import retry
from infra.env import NEED_PROXY, PROXIES, BITGET_DEMO
from infra.logger import log
from infra.util import get_time_ms


class BitgetClient(ExchangeAPI):
    HOST = "api.bitget.com"
    PRODUCT_TYPE = "USDT-FUTURES"

    def __init__(self, api_key: str, api_secret: str,
                 api_passphrase: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._demo = BITGET_DEMO
        self._session = requests.Session()
        headers = {
            "ACCESS-KEY": api_key,
            "ACCESS-PASSPHRASE": api_passphrase,
            "locale": "zh-CN",
            "Content-Type": "application/json",
        }
        if self._demo:
            headers["paptrading"] = "1"
        self._session.headers.update(headers)
        if NEED_PROXY:
            self._session.proxies.update(PROXIES)

    # ---- 签名 ----

    def _sign(self, method: str, path: str, query: str = "",
              body: str = "") -> dict:
        ts = get_time_ms()
        msg = ts + method + path + query + body
        mac = hmac.new(
            self._api_secret.encode(), msg.encode(), digestmod="sha256",
        )
        return {
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-SIGN": base64.b64encode(mac.digest()).decode(),
        }

    # ---- HTTP ----

    @retry(max_attempts=3, delay=2.0)
    def _get(self, path: str, query_str: str) -> Any:
        headers = self._sign("GET", path, "?" + query_str)
        url = f"https://{self.HOST}{path}?{query_str}"
        resp = self._session.get(url, headers=headers)
        if resp.status_code != 200:
            log.error("GET %s 返回 %d: %s", path, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()

    @retry(max_attempts=3, delay=2.0)
    def _post(self, path: str, data: dict) -> Any:
        body = json.dumps(data)
        log.debug("host:%s method:%s data:%s", self.HOST, path, body)
        headers = self._sign("POST", path, "", body)
        url = f"https://{self.HOST}{path}"
        resp = self._session.post(url, json=data, headers=headers)
        if resp.status_code != 200:
            log.error("POST %s 返回 %d: %s", path, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    @retry(max_attempts=5, delay=2.0)
    def _simple_get(url: str) -> requests.Response:
        if NEED_PROXY:
            return requests.get(url, proxies=PROXIES)
        return requests.get(url)

    # ---- 账户 ----

    def get_accounts(self, product_type: str) -> dict:
        return self._get("/api/v2/mix/account/accounts",
                         f"productType={product_type}")

    def set_leverage(self, symbol, product_type, margin_coin,
                     leverage=None, long_leverage=None,
                     short_leverage=None, hold_side=None) -> dict:
        data: dict = {"symbol": symbol, "marginCoin": margin_coin,
                      "productType": product_type}
        if leverage is not None:
            data["leverage"] = leverage
        if long_leverage is not None:
            data["longLeverage"] = long_leverage
        if short_leverage is not None:
            data["shortLeverage"] = short_leverage
        if hold_side is not None:
            data["holdSide"] = hold_side
        return self._post("/api/v2/mix/account/set-leverage", data)

    def open_count(self, symbol, product_type, margin_coin,
                   open_amount, open_price, leverage) -> dict:
        q = (f"productType={product_type}&symbol={symbol}"
             f"&marginCoin={margin_coin}&openAmount={open_amount}"
             f"&openPrice={open_price}&leverage={leverage}")
        return self._get("/api/v2/mix/account/open-count", q)

    # ---- 订单 ----

    def live_order(self, symbol, product_type, margin_mode, margin_coin,
                   side, size, order_type, trade_side, price="",
                   preset_stop_loss="") -> dict:
        data = {
            "symbol": symbol, "marginCoin": margin_coin, "size": size,
            "side": side, "orderType": order_type,
            "productType": product_type, "marginMode": margin_mode,
            "tradeSide": trade_side,
        }
        if order_type == "limit":
            data["price"] = str(price)
        if preset_stop_loss:
            data["presetStopLossPrice"] = preset_stop_loss
        return self._post("/api/v2/mix/order/place-order", data)

    def get_order_detail(self, symbol, product_type, order_id) -> dict:
        q = (f"orderId={order_id}&productType={product_type}"
             f"&symbol={symbol}")
        return self._get("/api/v2/mix/order/detail", q)

    def get_orders_pending(self, product_type) -> dict:
        return self._get("/api/v2/mix/order/orders-pending",
                         f"productType={product_type}")

    # ---- 持仓 ----

    def get_all_position(self, product_type) -> dict:
        resp = self._get("/api/v2/mix/position/all-position",
                         f"productType={product_type}")
        if resp.get("data") is None:
            return {"data": []}
        return resp

    def get_history_position(self, product_type, start_time) -> dict:
        q = f"productType={product_type}&startTime={start_time}"
        return self._get("/api/v2/mix/position/history-position", q)

    def get_fill_history(self, product_type, start_time) -> dict:
        q = f"productType={product_type}&startTime={start_time}"
        return self._get("/api/v2/mix/order/fill-history", q)

    # ---- 行情 ----

    def get_all_symbol(self, product_type) -> dict:
        url = (f"https://{self.HOST}/api/v2/mix/market/tickers"
               f"?productType={product_type}")
        resp = self._simple_get(url)
        return resp.json()

    def get_klines_url(self, symbol, product_type, granularity,
                       limit="100", end_time="") -> str:
        q = (f"symbol={symbol}&productType={product_type}"
             f"&granularity={granularity}&limit={limit}")
        if end_time:
            q += f"&endTime={end_time}"
        return f"https://{self.HOST}/api/v2/mix/market/candles?{q}"

    def get_history_fund_rate(self, symbol, product_type,
                              page_size="20") -> dict:
        q = (f"symbol={symbol}&productType={product_type}"
             f"&pageSize={page_size}")
        resp = self._simple_get(
            f"https://{self.HOST}/api/v2/mix/market/history-fund-rate?{q}")
        return resp.json()

    def get_contracts(self, symbol, product_type) -> dict:
        q = f"symbol={symbol}&productType={product_type}"
        resp = self._simple_get(
            f"https://{self.HOST}/api/v2/mix/market/contracts?{q}")
        return resp.json()

    # ---- 带单（Copy Trading） ----

    def copy_get_current_track(self, product_type, symbol="",
                               limit="20", id_less_than="",
                               id_greater_than="") -> dict:
        q = f"productType={product_type}"
        if symbol:
            q += f"&symbol={symbol}"
        if limit:
            q += f"&limit={limit}"
        if id_less_than:
            q += f"&idLessThan={id_less_than}"
        if id_greater_than:
            q += f"&idGreaterThan={id_greater_than}"
        return self._get("/api/v2/copy/mix-trader/order-current-track", q)

    def copy_get_history_track(self, product_type, symbol="",
                               limit="20", start_time="",
                               end_time="", id_less_than="",
                               id_greater_than="") -> dict:
        q = f"productType={product_type}"
        if symbol:
            q += f"&symbol={symbol}"
        if limit:
            q += f"&limit={limit}"
        if start_time:
            q += f"&startTime={start_time}"
        if end_time:
            q += f"&endTime={end_time}"
        if id_less_than:
            q += f"&idLessThan={id_less_than}"
        if id_greater_than:
            q += f"&idGreaterThan={id_greater_than}"
        return self._get("/api/v2/copy/mix-trader/order-history-track", q)

    def copy_close_track(self, tracking_no, symbol,
                         product_type) -> dict:
        return self._post("/api/v2/copy/mix-trader/order-close-track", {
            "trackingNo": tracking_no,
            "symbol": symbol,
            "productType": product_type,
        })

    def copy_modify_tpsl(self, tracking_no, symbol, product_type,
                         stop_profit_price="",
                         stop_loss_price="") -> dict:
        data = {
            "trackingNo": tracking_no,
            "symbol": symbol,
            "productType": product_type,
        }
        if stop_profit_price:
            data["stopProfitPrice"] = stop_profit_price
        if stop_loss_price:
            data["stopLossPrice"] = stop_loss_price
        return self._post(
            "/api/v2/copy/mix-trader/order-modify-track-tpsl", data)

    def copy_get_symbols(self, product_type) -> dict:
        return self._get("/api/v2/copy/mix-trader/config-query-symbols",
                         f"productType={product_type}")

    def copy_get_profit_summary(self) -> dict:
        return self._get("/api/v2/copy/mix-trader/config-query-settings",
                         "")
