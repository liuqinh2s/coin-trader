"""
通用工具函数：时间处理
"""
import datetime
import time

import pytz

_TZ_SHANGHAI = pytz.timezone("Asia/Shanghai")


def get_timestamp(date_str: str, fmt: str = "%Y-%m-%d %H:%M") -> int:
    """将日期字符串转为毫秒时间戳（上海时区）"""
    dt = datetime.datetime.strptime(date_str, fmt)
    dt_aware = _TZ_SHANGHAI.localize(dt)
    return int(dt_aware.timestamp()) * 1000


def get_time_ms() -> str:
    """当前毫秒时间戳（字符串）"""
    return str(int(time.time() * 1000))


def get_human_time(ts_ms: str = "") -> str:
    """毫秒时间戳转可读时间字符串，不传参则返回当前时间"""
    if ts_ms:
        dt = datetime.datetime.fromtimestamp(float(ts_ms) / 1000)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


# 保留旧名称兼容
getTimeStamp = get_timestamp
getTime = get_time_ms
getHumanReadTime = get_human_time
