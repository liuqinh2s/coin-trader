"""RSI（相对强弱指数）计算"""
import pandas as pd


def calculate_rsi(data, period: int = 14) -> list[float]:
    """
    计算 RSI 指标

    :param data:   价格序列（list 或 pd.Series）
    :param period: RSI 周期，默认 14
    :return: RSI 值列表（前 period 个为 NaN）
    """
    if not isinstance(data, pd.Series):
        data = pd.Series(data)

    delta = data.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.values.tolist()
