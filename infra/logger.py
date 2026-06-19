"""
日志框架：统一使用 logging 模块，钉钉作为 handler 推送重要信息
"""
from __future__ import annotations

import logging

from infra.send_msg import send_dingtalk


class DingTalkHandler(logging.Handler):
    """将 WARNING 及以上级别的日志推送到钉钉"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            send_dingtalk(msg)
        except Exception:
            self.handleError(record)


def setup_logger(name: str = "bitget_bot", level: int = logging.DEBUG) -> logging.Logger:
    """创建并配置 logger"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(level)

    # 控制台 handler — DEBUG 及以上
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(console)

    # 钉钉 handler — WARNING 及以上
    dt = DingTalkHandler()
    dt.setLevel(logging.WARNING)
    dt.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(dt)

    return logger


# 全局 logger 实例
log = setup_logger()


def notify(msg: str) -> None:
    """主动推送到钉钉（不受日志级别限制），同时记录 INFO"""
    send_dingtalk(msg)
    log.info(msg)
