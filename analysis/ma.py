"""简单移动平均线（SMA）计算"""
import pandas as pd


def moving_average_np(data, window_size: int) -> list[float]:
    """
    计算简单移动平均线

    :param data:        价格序列（list 或 pd.Series）
    :param window_size: 窗口大小
    """
    if not isinstance(data, pd.Series):
        data = pd.Series(data)
    return data.rolling(window=window_size).mean().values.tolist()
