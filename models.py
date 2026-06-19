"""
数据模型：Candle（K 线）、AccountState（账户状态）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Candle:
    """单根 K 线"""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float

    @classmethod
    def from_raw(cls, raw: list) -> Candle:
        """从 API 返回的原始列表构造"""
        return cls(
            timestamp=int(raw[0]),
            open=float(raw[1]),
            high=float(raw[2]),
            low=float(raw[3]),
            close=float(raw[4]),
            volume=float(raw[5]),
            quote_volume=float(raw[6]),
        )

    @classmethod
    def from_raw_list(cls, raw_list: list[list]) -> list[Candle]:
        """批量转换"""
        return [cls.from_raw(r) for r in raw_list]


class AccountState:
    """
    账户状态管理，替代全局 account 字典。
    所有状态变更通过方法完成，避免隐式共享。
    """

    def __init__(self, initial_balance: float = 1000.0) -> None:
        # 余额
        self.balance: float = initial_balance
        self.position_balance: float = initial_balance
        self.largest_balance: float = initial_balance
        self.max_drawdown: float = 0.0

        # 持仓
        self.position: dict[str, Any] = {}
        self.position_symbol: str = ""
        self.position_type: str = ""  # "BUY" | ""
        self.position_price: float = 0.0

        # 盈亏统计
        self.long_profit_count: int = 0
        self.long_loss_count: int = 0
        self.long_profit: float = 0.0
        self.long_loss: float = 0.0

        # 持仓时间统计（毫秒）
        self.no_position_time: int = 0
        self.all_no_position_time: int = 0
        self.long_position_time: int = 0
        self.all_long_position_time: int = 0

        # 选币结果
        self.buy_list: dict[str, str] = {}

        # 风控
        self.price_track: dict[str, dict] = {}
        self.is_shutdown: bool = False
        self.shutdown_position: int = 0
        self.is_first_scan_position: bool = True

    def update_balance(self, new_balance: float) -> None:
        """更新余额并刷新资产峰值"""
        self.balance = new_balance
        self.position_balance = new_balance
        if new_balance > self.largest_balance:
            self.largest_balance = new_balance

    def update_drawdown(self, profit: float) -> None:
        """平仓后更新余额和最大回撤"""
        self.balance += profit
        drawdown = (self.largest_balance - self.balance) / self.largest_balance
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown
        if self.balance > self.largest_balance:
            self.largest_balance = self.balance

    def record_profit(self, profit: float, order_type: str) -> None:
        """记录盈亏统计"""
        if profit > 0:
            self.long_profit_count += 1
            self.long_profit += profit
        elif profit < 0:
            self.long_loss_count += 1
            self.long_loss += profit

    def reset_position_time(self) -> int:
        """重置持仓时间，返回本次持仓时长"""
        duration = self.long_position_time
        self.all_long_position_time += duration
        self.long_position_time = 0
        return duration

    def reset_no_position_time(self) -> int:
        """重置空仓时间，返回本次空仓时长"""
        duration = self.no_position_time
        self.all_no_position_time += duration
        self.no_position_time = 0
        return duration
