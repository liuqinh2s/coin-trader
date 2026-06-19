"""MACD 指标计算"""
import pandas as pd


def _ema(data: pd.Series, span: int) -> pd.Series:
    """指数移动平均线（EMA）"""
    return data.ewm(span=span, adjust=False).mean()


# 保留旧名称兼容
calculate_ema = _ema


def calculate_macd(
    data,
    short_span: int = 12,
    long_span: int = 26,
    signal_span: int = 9,
) -> dict[str, list[float]]:
    """
    计算 MACD 指标

    :param data:        价格序列（list 或 pd.Series）
    :param short_span:  短期 EMA 周期（默认 12）
    :param long_span:   长期 EMA 周期（默认 26）
    :param signal_span: 信号线周期（默认 9）
    :return: {'MACD_Line': [...], 'Signal_Line': [...], 'Histogram': [...]}
    """
    if not isinstance(data, pd.Series):
        data = pd.Series(data)

    macd_line = _ema(data, short_span) - _ema(data, long_span)
    signal_line = _ema(macd_line, signal_span)

    return {
        "MACD_Line": macd_line.values.tolist(),
        "Signal_Line": signal_line.values.tolist(),
        "Histogram": (macd_line - signal_line).values.tolist(),
    }
