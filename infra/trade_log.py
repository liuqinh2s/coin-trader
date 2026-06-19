"""
交易日志模块：将每笔开仓/平仓记录写入 CSV 文件，便于事后分析策略表现
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

from infra.logger import log
from infra.util import get_human_time, get_time_ms

# 日志文件路径
_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_TRADE_LOG_FILE = _LOG_DIR / "trades.csv"

# CSV 表头
_OPEN_FIELDS = [
    "时间", "类型", "币种", "开仓价", "开仓量(u)", "持仓量",
    "手续费", "杠杆", "选币原因", "加分项", "账户余额",
]

_CLOSE_FIELDS = [
    "时间", "类型", "币种", "平仓价", "开仓价", "持仓量",
    "手续费", "盈亏(USDT)", "盈亏(%)", "持仓时长(小时)",
    "最高浮盈(%)", "平仓原因", "账户余额",
]

# 合并所有字段（CSV 用统一表头，缺失字段留空）
_ALL_FIELDS = [
    "时间", "类型", "币种", "开仓价", "平仓价", "开仓量(u)", "持仓量",
    "手续费", "杠杆", "盈亏(USDT)", "盈亏(%)", "持仓时长(小时)",
    "最高浮盈(%)", "选币原因", "加分项", "平仓原因", "账户余额",
]


def _ensure_file() -> None:
    """确保日志目录和 CSV 文件存在，不存在则创建并写入表头"""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not _TRADE_LOG_FILE.exists():
        with open(_TRADE_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_ALL_FIELDS)
            writer.writeheader()


def _append_row(row: dict) -> None:
    """追加一行到 CSV"""
    _ensure_file()
    try:
        with open(_TRADE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_ALL_FIELDS, extrasaction="ignore")
            writer.writerow(row)
    except Exception as e:
        log.warning("写入交易日志失败: %s", e)


def log_open(
    symbol: str,
    filled_price: float,
    quote_volume: str,
    base_volume: str,
    fee: str,
    leverage: int,
    reason: str,
    bonus: list[str],
    balance: float,
    ctime: str = "",
) -> None:
    """记录开仓"""
    _append_row({
        "时间": get_human_time(ctime) if ctime else get_human_time(),
        "类型": "开多",
        "币种": symbol,
        "开仓价": f"{filled_price:.6g}",
        "开仓量(u)": quote_volume,
        "持仓量": base_volume,
        "手续费": fee,
        "杠杆": f"{leverage}x",
        "选币原因": reason,
        "加分项": ", ".join(bonus) if bonus else "无",
        "账户余额": f"{balance:.2f}",
    })
    log.info(
        "📝 交易日志[开仓] %s 价格=%s 原因=%s 加分=%s",
        symbol, filled_price, reason, bonus,
    )


def log_close(
    symbol: str,
    close_price: float,
    open_price: float,
    base_volume: str,
    fee: str,
    profit: float,
    hold_hours: float,
    max_floating_pct: float,
    close_reason: str,
    balance: float,
    ctime: str = "",
) -> None:
    """记录平仓"""
    pnl_pct = (close_price - open_price) / open_price * 100 if open_price else 0
    _append_row({
        "时间": get_human_time(ctime) if ctime else get_human_time(),
        "类型": "平多",
        "币种": symbol,
        "平仓价": f"{close_price:.6g}",
        "开仓价": f"{open_price:.6g}",
        "持仓量": base_volume,
        "手续费": fee,
        "盈亏(USDT)": f"{profit:.4f}",
        "盈亏(%)": f"{pnl_pct:.2f}%",
        "持仓时长(小时)": f"{hold_hours:.1f}",
        "最高浮盈(%)": f"{max_floating_pct:.2f}%",
        "平仓原因": close_reason,
        "账户余额": f"{balance:.2f}",
    })
    log.info(
        "📝 交易日志[平仓] %s 盈亏=%.4f USDT (%.2f%%) 持仓=%.1f小时 "
        "最高浮盈=%.2f%% 原因=%s",
        symbol, profit, pnl_pct, hold_hours, max_floating_pct, close_reason,
    )
