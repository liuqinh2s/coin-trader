"""
带单管理模块：监控带单状态、同步止盈止损、汇报跟单信息

作为交易员（带单员），机器人正常下单后 Bitget 会自动广播给跟单者。
本模块负责：
    - 监控当前带单列表，确保与持仓同步
    - 在策略触发平仓时，通过带单 API 平仓（确保跟单者同步平仓）
    - 同步止盈止损到带单订单
    - 定期汇报带单收益
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from api.factory import get_exchange
from infra.config import get_config
from infra.logger import log

if TYPE_CHECKING:
    from models import AccountState


def get_current_tracks() -> list[dict]:
    """获取当前所有带单订单"""
    ex = get_exchange()
    try:
        resp = ex.copy_get_current_track(ex.PRODUCT_TYPE, limit="50")
        if resp.get("code") != "00000":
            log.warning("获取当前带单失败: %s", resp.get("msg"))
            return []
        return resp.get("data", {}).get("trackingList", []) or []
    except Exception as e:
        log.warning("获取当前带单异常: %s", e)
        return []


def close_track_by_symbol(symbol: str) -> bool:
    """
    带单平仓（兼容新版 Bitget 带单系统）。

    Bitget 已将带单升级为 position-based 模型，交易员直接通过普通交易 API
    平仓即可，跟单者会自动按比例同步平仓，无需再调用旧的
    /api/v2/copy/mix-trader/order-close-track 接口。

    本函数保留用于记录日志，实际平仓由 order.py 中的 live_order 完成。
    """
    tracks = get_current_tracks()
    has_track = any(t.get("symbol") == symbol for t in tracks)
    if has_track:
        log.info("带单模式: %s 将通过普通平仓同步跟单者", symbol)
    else:
        log.info("带单模式: %s 无活跃带单订单", symbol)
    return has_track


def sync_tpsl_to_track(symbol: str, stop_profit: str = "",
                       stop_loss: str = "") -> None:
    """将止盈止损同步到带单订单"""
    if not stop_profit and not stop_loss:
        return

    ex = get_exchange()
    tracks = get_current_tracks()

    for track in tracks:
        if track.get("symbol") != symbol:
            continue
        tracking_no = track["trackingNo"]
        try:
            resp = ex.copy_modify_tpsl(
                tracking_no, symbol, ex.PRODUCT_TYPE,
                stop_profit, stop_loss,
            )
            if resp.get("code") == "00000":
                log.info("带单止盈止损同步: %s tp=%s sl=%s",
                         symbol, stop_profit, stop_loss)
            else:
                log.warning("带单止盈止损同步失败: %s %s",
                            tracking_no, resp.get("msg"))
        except Exception as e:
            log.warning("带单止盈止损同步异常: %s - %s", symbol, e)


def report_copy_trading_status() -> None:
    """汇报当前带单状态：当前带单数、跟单人数等"""
    tracks = get_current_tracks()
    if not tracks:
        log.info("当前无带单订单")
        return

    total_followers = 0
    lines = [f"当前带单 {len(tracks)} 笔:"]
    for t in tracks:
        followers = int(t.get("followCount", 0))
        total_followers += followers
        side_cn = "多" if t["posSide"] == "long" else "空"
        lines.append(
            f"  {t['symbol']} {side_cn} "
            f"杠杆:{t['openLeverage']}x "
            f"均价:{t['openPriceAvg']} "
            f"数量:{t['openSize']} "
            f"跟单:{followers}人"
        )
    lines.append(f"总跟单人数: {total_followers}")
    log.info("\n".join(lines))


def report_history_summary(limit: str = "20") -> None:
    """汇报历史带单收益"""
    ex = get_exchange()
    try:
        resp = ex.copy_get_history_track(ex.PRODUCT_TYPE, limit=limit)
        if resp.get("code") != "00000":
            log.warning("获取历史带单失败: %s", resp.get("msg"))
            return
        tracks = resp.get("data", {}).get("trackingList", []) or []
        if not tracks:
            log.info("暂无历史带单记录")
            return

        total_pl = sum(float(t.get("achievedPL", 0)) for t in tracks)
        win = sum(1 for t in tracks if float(t.get("achievedPL", 0)) > 0)
        lose = sum(1 for t in tracks if float(t.get("achievedPL", 0)) < 0)
        log.info(
            "最近 %d 笔带单: 盈亏=%.4f USDT, 盈利 %d 笔, 亏损 %d 笔, 胜率=%.1f%%",
            len(tracks), total_pl, win, lose, win / len(tracks) * 100,
        )
    except Exception as e:
        log.warning("获取历史带单异常: %s", e)
