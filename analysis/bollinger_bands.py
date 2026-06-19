"""布林带（Bollinger Bands）计算"""
import pandas as pd


def calculate_bollinger_bands(
    data, window: int = 20, num_std: int = 2
) -> dict[str, list[float]]:
    """
    计算布林带

    :param data:    价格序列（list 或 pd.Series）
    :param window:  移动平均窗口，默认 20
    :param num_std: 标准差倍数，默认 2
    :return: {'Middle Band': [...], 'Upper Band': [...], 'Lower Band': [...]}
    """
    if not isinstance(data, pd.Series):
        data = pd.Series(data)

    middle = data.rolling(window=window).mean()
    std = data.rolling(window=window).std(ddof=0)

    return {
        "Middle Band": middle.values.tolist(),
        "Upper Band": (middle + std * num_std).values.tolist(),
        "Lower Band": (middle - std * num_std).values.tolist(),
    }
