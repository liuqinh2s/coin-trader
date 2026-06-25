"""
下单模块：开仓、平仓、统一下单入口
"""
from __future__ import annotations

from decimal import Decimal
from time import sleep
from typing import TYPE_CHECKING

from api.factory import get_exchange
from infra.config import get_config
from infra.logger import log, notify
from infra.trade_log import log_open, log_close
from infra.util import get_human_time, get_time_ms
from core.copy_trading import close_track_by_symbol, sync_tpsl_to_track
from core.margin import estimate_extra_isolated_margin
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
    """将交易所返回的数值安全转成 Decimal。"""
    return Decimal(str(value))


def _format_size(size: Decimal) -> str:
    """格式化下单数量，避免科学计数法。"""
    return format(size.normalize(), "f")


def _format_usdt_amount(amount: float) -> str:
    dec = Decimal(str(amount)).quantize(Decimal("0.00000001"))
    return format(dec.normalize(), "f")


def _add_margin_to_protect_stop(
    symbol: str,
    filled_price: float,
    filled_size: float,
    stop_price: float,
    state: AccountState,
) -> dict:
    cfg = get_config()
    auto_cfg = cfg.get("auto_trade", {})
    leverage = int(cfg.get("leverage", 10))
    min_extra_margin = float(auto_cfg.get("min_extra_margin_usdt", 0.1))
    plan = estimate_extra_isolated_margin(
        filled_price, filled_size, stop_price, leverage, state.balance, auto_cfg,
    )
    result = {
        "extra_margin_usdt": plan.extra_margin,
        "required_margin_usdt": plan.required_margin,
        "initial_margin_usdt": plan.initial_margin,
        "target_liquidation_price": plan.target_liquidation_price,
        "extra_margin_capped": plan.capped,
        "extra_margin_added": False,
    }
    if plan.extra_margin < min_extra_margin:
        log.info("%s ATR stop protection does not need extra margin: %.8f USDT", symbol, plan.extra_margin)
        return result
    if plan.capped:
        log.warning(
            "%s ATR stop protection extra margin capped at %.4f USDT",
            symbol, plan.extra_margin,
        )
    try:
        amount = _format_usdt_amount(plan.extra_margin)
        resp = get_exchange().set_position_margin(
            symbol, get_exchange().PRODUCT_TYPE, "USDT", amount, "long",
        )
        log.info("%s added isolated margin for ATR stop protection: %s USDT %s", symbol, amount, resp)
        result["extra_margin_added"] = True
        result["extra_margin_response"] = resp
    except Exception as exc:
        log.warning("%s failed to add isolated margin for ATR stop protection: %s", symbol, exc)
        result["extra_margin_error"] = str(exc)
    return result


def close_position(symbol: str, state: AccountState,
                   close_reason: str = "手动平仓",
                   close_size=None) -> float:
    """
    平多仓，可指定 close_size 做部分平仓。
    :param close_reason: 平仓原因（由 cut_profit 等调用方传入）
    :param close_size: 不传则全平；传入时按指定数量部分平仓
    :return: 本次盈亏
    """
    cfg = get_config()
    ex = get_exchange()

    # 带单模式：先通过带单 API 平仓，确保跟单者同步
    if cfg.get("copy_trading_enabled", False):
        close_track_by_symbol(symbol)
    available = _to_decimal(state.position[symbol]["available"])
    size = available if close_size is None else min(_to_decimal(close_size), available)
    if size <= 0:
        log.warning("%s 可平仓数量为 0，跳过平仓", symbol)
        return 0.0
    is_full_close = size >= available
    size_str = _format_size(size)
    action_name = "平多" if is_full_close else "部分平多"
    log.info("下单量：%su  %s", size_str, action_name)

    order_info = ex.live_order(
        symbol, ex.PRODUCT_TYPE, "isolated", "USDT",
        "buy", size_str, "market", "close",
    )
    log.info("orderInfo: %s", order_info)
    detail = _wait_for_filled(symbol, order_info)
    log.info("orderDetail: %s", detail)

    profit = float(detail["data"]["totalProfits"])
    close_price = float(detail["data"]["priceAvg"])
    open_price = float(state.position[symbol]["openPriceAvg"])
    c_time = int(state.position[symbol]["cTime"])
    hold_hours = (int(get_time_ms()) - c_time) / 1000 / 3600

    # 最高浮盈
    max_floating_pct = 0.0
    track = state.price_track.get(symbol)
    if track and open_price > 0:
        max_floating_pct = (track["priceHigh"] - open_price) / open_price * 100

    notify(
        f"时间: {get_human_time(detail['data']['cTime'])} {symbol} {action_name}, "
        f"价格: {detail['data']['priceAvg']} "
        f"持仓量:{detail['data']['baseVolume']} "
        f"手续费:{detail['data']['fee']} 盈亏: {profit}"
    )

    # 写入交易日志
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
    )

    state.update_drawdown(profit)

    log.info("当前最大回撤：%s", state.max_drawdown)
    log.info("资产最高峰：%s", state.largest_balance)
    log.info("账户总额：%s", state.balance)

    if is_full_close:
        # 从内存中移除已平仓位
        state.position.pop(symbol, None)
        state.price_track.pop(symbol, None)
        state.position_type = ""
        remove_position_risk(symbol)
        # 止盈/止损后进入冷却，冷却期内不再买入该币
        state.cooldown[symbol] = int(get_time_ms())

        duration = state.reset_position_time()
        log.info("做多天数：%s", _ms_to_days(duration))
        log.info("总做多天数: %s", _ms_to_days(state.all_long_position_time))
    else:
        filled_size = _to_decimal(detail["data"].get("baseVolume", size_str))
        remaining = max(available - filled_size, Decimal("0"))
        remaining_str = _format_size(remaining)
        state.position[symbol]["available"] = remaining_str
        if "total" in state.position[symbol]:
            state.position[symbol]["total"] = remaining_str
        log.info("%s 部分平仓后剩余持仓量：%s", symbol, remaining_str)

    state.position_balance = state.balance
    return profit


def open_position(symbol: str, price: float, state: AccountState,
                  reason: str = "", bonus: list[str] | None = None,
                  size=None, preset_stop_loss: str = "",
                  risk_info: dict | None = None) -> None:
    """开多仓"""
    ex = get_exchange()
    cfg = get_config()
    leverage = cfg.get("leverage", 10)
    leverage_info = ex.set_leverage(
        symbol, ex.PRODUCT_TYPE, "USDT", None,
        leverage, None, "long",
    )
    log.info("调整杠杆：%s", leverage_info)

    min_usdt = cfg.get("min_usdt", 10)
    position_balance = min_usdt if state.is_shutdown else state.position_balance
    order_size = size if size is not None else position_balance / price
    log.info("下单数量：%s  开多 预设止损:%s", order_size, preset_stop_loss or "无")

    order_info = ex.live_order(
        symbol, ex.PRODUCT_TYPE, "isolated", "USDT",
        "buy", order_size, "market", "open",
        preset_stop_loss=preset_stop_loss,
    )
    log.info("orderInfo: %s", order_info)
    detail = _wait_for_filled(symbol, order_info)
    log.info("orderDetail: %s", detail)

    filled_price = float(detail["data"]["priceAvg"])
    filled_size = float(detail["data"]["baseVolume"])

    if risk_info:
        stop_price = float(risk_info.get("stop_price", preset_stop_loss or 0))
        actual_risk = max(filled_price - stop_price, 0) * filled_size
        margin_protection = _add_margin_to_protect_stop(
            symbol, filled_price, filled_size, stop_price, state,
        )
        record_position_risk(symbol, {
            **risk_info,
            "symbol": symbol,
            "open_price": filled_price,
            "base_volume": filled_size,
            "quote_volume": float(detail["data"].get("quoteVolume", filled_price * filled_size)),
            "actual_risk_usdt": actual_risk,
            "stop_price": stop_price,
            "margin_protection": margin_protection,
        })

    # 写入内存持仓，避免再次从服务器拉取
    state.position[symbol] = {
        "symbol": symbol,
        "holdSide": "long",
        "openPriceAvg": detail["data"]["priceAvg"],
        "available": detail["data"]["baseVolume"],
        "cTime": detail["data"]["cTime"],
    }

    state.position_type = "BUY"
    state.position_symbol = symbol

    # 带单模式：同步止盈止损到带单订单
    if cfg.get("copy_trading_enabled", False):
        sync_tpsl_to_track(symbol, "", "")

    duration = state.reset_no_position_time()
    log.info("空仓天数：%s", _ms_to_days(duration))
    log.info("总空仓天数: %s", _ms_to_days(state.all_no_position_time))

    # 组装开仓原因信息
    reason_info = f"原因: {reason}" if reason else "原因: 无"
    bonus_info = f" 加分项: {', '.join(bonus)}" if bonus else ""
    notify(
        f"时间: {get_human_time(detail['data']['cTime'])} {symbol} 开多, "
        f"价格: {filled_price} 开仓量:{detail['data']['quoteVolume']}u "
        f"持仓量:{detail['data']['baseVolume']} 手续费:{detail['data']['fee']} "
        f"{reason_info}{bonus_info}"
    )

    # 写入交易日志
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
    )


def order(symbol: str, data: list, order_type: str,
          state: AccountState, only_close: bool = False,
          cut: dict | None = None,
          reason: str = "", bonus: list[str] | None = None,
          close_reason: str = "", close_size=None,
          size=None, preset_stop_loss: str = "",
          risk_info: dict | None = None) -> float | None:
    """
    统一下单入口

    :param symbol:       交易对
    :param data:         K 线数据列表
    :param order_type:   'BUY'（开多）或 'SELL'（平多）
    :param state:        账户状态
    :param only_close:   True 时只平仓不开新仓
    :param reason:       开仓选币原因
    :param bonus:        开仓加分项列表
    :param close_reason: 平仓原因
    :param close_size:   SELL 时指定部分平仓数量；不传则全平
    """
    price = float(data[-1][4])
    profit = 0.0

    try:
        if order_type == "BUY":
            pos = state.position.get(symbol)
            if pos and pos["holdSide"] == "long":
                return 0.0  # 已持有多仓
            if not only_close:
                open_position(
                    symbol, price, state, reason=reason, bonus=bonus,
                    size=size, preset_stop_loss=preset_stop_loss,
                    risk_info=risk_info,
                )
        else:  # SELL = 平多
            pos = state.position.get(symbol)
            if pos and pos["holdSide"] == "long":
                profit = close_position(
                    symbol, state,
                    close_reason=close_reason or "未知",
                    close_size=close_size,
                )

        state.record_profit(profit, order_type)
        return profit
    except TimeoutError as e:
        log.error("order 超时: %s %s - %s", symbol, order_type, e)
        notify(f"下单超时: {symbol} {order_type} - {e}")
    except KeyError as e:
        log.error("order 数据缺失: %s %s - %s", symbol, order_type, e)
    except (ConnectionError, OSError) as e:
        log.error("order 网络异常: %s %s - %s", symbol, order_type, e)
        notify(f"下单网络异常: {symbol} {order_type} - {e}")
    except Exception as e:
        log.error("order 未知异常: %s %s - %s", symbol, order_type, e)
    return None
