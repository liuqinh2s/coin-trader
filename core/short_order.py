"""
做空下单模块：开空、平空。

与做多的 core.order 完全独立，避免相互影响。
开空时挂交易所预设止盈单（价格下跌 take_profit_pct 即自动平空止盈），
不设止损，10x 杠杆用爆仓兜底。
"""
from __future__ import annotations

from decimal import Decimal
from time import sleep
from typing import TYPE_CHECKING

from api.errors import ExchangeBusinessError
from api.factory import get_exchange
from infra.config import get_config
from infra.logger import log, notify
from infra.trade_log import log_open, log_close
from infra.util import get_human_time, get_time_ms
from core.risk_cache import record_position_risk, remove_position_risk

if TYPE_CHECKING:
    from models import AccountState


def _wait_for_filled(symbol: str, order_info: dict) -> dict:
    """市价单等待完全成交，每 5 秒轮询一次"""
    ex = get_exchange()
    sleep(5)
    for _ in range(60):  # 最多等 5 分钟
        detail = ex.get_order_detail(symbol, ex.PRODUCT_TYPE, order_info["data"]["orderId"])
        if detail["data"]["state"] == "filled":
            return detail
        sleep(5)
    raise TimeoutError(f"{symbol} 订单未在 5 分钟内成交")


def _ms_to_days(ms: int | float) -> float:
    return ms / 1000 / 60 / 60 / 24


def _to_decimal(value) -> Decimal:
    return Decimal(str(value))


def _format_size(size: Decimal) -> str:
    return format(size.normalize(), "f")


def open_short(symbol: str, price: float, size: str, state: AccountState,
               reason: str = "", bonus: list[str] | None = None,
               preset_take_profit: str = "",
               risk_info: dict | None = None) -> None:
    """开空仓，可携带交易所预设止盈单。"""
    ex = get_exchange()
    leverage = 10
    leverage_info = ex.set_leverage(
        symbol, ex.PRODUCT_TYPE, "USDT", None,
        None, leverage, "short",
    )
    log.info("调整杠杆（空）：%s", leverage_info)

    log.info("下单数量：%s  开空 预设止盈=%s", size, preset_take_profit or "无")
    order_info = ex.live_order(
        symbol, ex.PRODUCT_TYPE, "isolated", "USDT",
        "sell", size, "market", "open",
        preset_take_profit=preset_take_profit,
    )
    log.info("orderInfo: %s", order_info)
    detail = _wait_for_filled(symbol, order_info)
    log.info("orderDetail: %s", detail)

    filled_price = float(detail["data"]["priceAvg"])
    filled_size = float(detail["data"]["baseVolume"])

    if risk_info:
        record_position_risk(symbol, {
            **risk_info,
            "symbol": symbol,
            "open_price": filled_price,
            "base_volume": filled_size,
            "quote_volume": float(detail["data"].get("quoteVolume", filled_price * filled_size)),
            "take_profit_price": preset_take_profit,
        })

    # 写入内存持仓
    state.position[symbol] = {
        "symbol": symbol,
        "holdSide": "short",
        "marginMode": "isolated",
        "openPriceAvg": detail["data"]["priceAvg"],
        "available": detail["data"]["baseVolume"],
        "cTime": detail["data"]["cTime"],
    }
    state.position_type = "SHORT"
    state.position_symbol = symbol

    reason_info = f"原因: {reason}" if reason else "原因: 无"
    bonus_info = f" 标签: {', '.join(bonus)}" if bonus else ""
    notify(
        f"时间: {get_human_time(detail['data']['cTime'])} {symbol} 开空, "
        f"价格: {filled_price} 开仓量:{detail['data']['quoteVolume']}u "
        f"持仓量:{detail['data']['baseVolume']} 手续费:{detail['data']['fee']} "
        f"{reason_info}{bonus_info}"
    )

    log_open(
        symbol=symbol,
        filled_price=filled_price,
        quote_volume=detail["data"]["quoteVolume"],
        base_volume=detail["data"]["baseVolume"],
        fee=detail["data"]["fee"],
        leverage=leverage,
        reason=reason,
        bonus=bonus or [],
        balance=state.balance,
        ctime=detail["data"]["cTime"],
        action="开空",
    )


def close_short(symbol: str, state: AccountState,
                close_reason: str = "手动平空") -> float:
    """全平空仓，返回本次盈亏。"""
    ex = get_exchange()

    available = _to_decimal(state.position[symbol]["available"])
    if available <= 0:
        log.warning("%s 可平仓数量为 0，跳过平空", symbol)
        return 0.0
    size_str = _format_size(available)
    log.info("下单量：%s  平空", size_str)

    margin_mode = state.position[symbol].get("marginMode", "isolated")
    order_info = ex.live_order(
        symbol, ex.PRODUCT_TYPE, margin_mode, "USDT",
        "sell", size_str, "market", "close",
    )
    log.info("orderInfo: %s", order_info)
    detail = _wait_for_filled(symbol, order_info)
    log.info("orderDetail: %s", detail)

    profit = float(detail["data"]["totalProfits"])
    close_price = float(detail["data"]["priceAvg"])
    open_price = float(state.position[symbol]["openPriceAvg"])
    c_time = int(state.position[symbol]["cTime"])
    hold_hours = (int(get_time_ms()) - c_time) / 1000 / 3600

    # 做空最高浮盈：最低价相对开仓价的跌幅
    max_floating_pct = 0.0
    track = state.price_track.get(symbol)
    if track and open_price > 0:
        max_floating_pct = (open_price - track["priceLow"]) / open_price * 100

    # 做空盈亏方向与做多相反：跌 = 盈
    pnl_pct = (open_price - close_price) / open_price * 100 if open_price else 0

    notify(
        f"时间: {get_human_time(detail['data']['cTime'])} {symbol} 平空, "
        f"价格: {detail['data']['priceAvg']} "
        f"持仓量:{detail['data']['baseVolume']} "
        f"手续费:{detail['data']['fee']} 盈亏: {profit}"
    )

    log_close(
        symbol=symbol,
        close_price=close_price,
        open_price=open_price,
        base_volume=detail["data"]["baseVolume"],
        fee=detail["data"]["fee"],
        profit=profit,
        hold_hours=hold_hours,
        max_floating_pct=max_floating_pct,
        close_reason=close_reason,
        balance=state.balance + profit,
        ctime=detail["data"]["cTime"],
        action="平空",
        pnl_pct=pnl_pct,
    )

    state.update_drawdown(profit)
    log.info("当前最大回撤：%s", state.max_drawdown)
    log.info("账户总额：%s", state.balance)

    # 从内存移除已平仓位，进入冷却
    state.position.pop(symbol, None)
    state.price_track.pop(symbol, None)
    state.position_type = ""
    remove_position_risk(symbol)
    state.cooldown[symbol] = int(get_time_ms())

    state.position_balance = state.balance
    return profit


def short_order(symbol: str, data: list, order_type: str,
                state: AccountState, size: str = "",
                reason: str = "", bonus: list[str] | None = None,
                close_reason: str = "",
                preset_take_profit: str = "",
                risk_info: dict | None = None) -> float | None:
    """做空统一下单入口。

    :param order_type: 'OPEN'（开空）或 'CLOSE'（平空）
    """
    price = float(data[-1][4])
    profit = 0.0

    try:
        if order_type == "OPEN":
            pos = state.position.get(symbol)
            if pos and pos.get("holdSide") == "short":
                return 0.0  # 已持有空仓
            open_short(
                symbol, price, size, state, reason=reason, bonus=bonus,
                preset_take_profit=preset_take_profit, risk_info=risk_info,
            )
        else:  # CLOSE = 平空
            pos = state.position.get(symbol)
            if pos and pos.get("holdSide") == "short":
                profit = close_short(
                    symbol, state, close_reason=close_reason or "未知",
                )

        state.record_profit(profit, order_type)
        return profit
    except ExchangeBusinessError as e:
        if e.code == "22002":
            log.error("short_order %s %s: 交易所拒绝平仓(22002-暂无仓位可平)",
                      symbol, order_type)
            notify(f"{symbol} 平空失败(22002)，请检查仓位保证金模式")
        else:
            log.error("short_order 交易所错误: %s %s - %s", symbol, order_type, e)
            notify(f"做空下单交易所错误: {symbol} {order_type} - {e}")
    except TimeoutError as e:
        log.error("short_order 超时: %s %s - %s", symbol, order_type, e)
        notify(f"做空下单超时: {symbol} {order_type} - {e}")
    except KeyError as e:
        log.error("short_order 数据缺失: %s %s - %s", symbol, order_type, e)
    except (ConnectionError, OSError) as e:
        log.error("short_order 网络异常: %s %s - %s", symbol, order_type, e)
        notify(f"做空下单网络异常: {symbol} {order_type} - {e}")
    except Exception as e:
        log.error("short_order 未知异常: %s %s - %s", symbol, order_type, e)
    return None
