"""
重试装饰器：统一处理网络异常与限流
"""
from __future__ import annotations

import functools
import time
from typing import Callable, TypeVar

import requests

from infra.logger import log

F = TypeVar("F", bound=Callable)

_RETRYABLE = (ConnectionError, TimeoutError, OSError, requests.exceptions.HTTPError)


def _get_retry_delay(exc: Exception, default_wait: float) -> float:
    """根据异常类型决定等待时间，429 时优先使用 Retry-After 头"""
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        if exc.response.status_code == 429:
            retry_after = exc.response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(float(retry_after), default_wait)
                except ValueError:
                    pass
            # Bitget 429 没有 Retry-After 头时，至少等 5 秒
            return max(5.0, default_wait)
        # 5xx 服务端错误也值得重试
        if exc.response.status_code >= 500:
            return default_wait
        # 其他 4xx（如 401/403）不应重试，直接抛出
        raise exc
    return default_wait


def retry(
    max_attempts: int = 3,
    delay: float = 2.0,
    backoff: float = 2.0,
    exceptions: tuple = _RETRYABLE,
) -> Callable[[F], F]:
    """
    重试装饰器

    :param max_attempts: 最大重试次数
    :param delay:        初始延迟（秒）
    :param backoff:      延迟倍增系数
    :param exceptions:   需要重试的异常类型（含 HTTPError）
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            wait = delay
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    try:
                        wait = _get_retry_delay(e, wait)
                    except requests.exceptions.HTTPError:
                        raise e  # 非可重试的 HTTP 错误，直接抛出
                    log.warning(
                        "%s 第 %d/%d 次重试，%.1f秒后重试，异常: %s",
                        func.__name__, attempt, max_attempts, wait, e,
                    )
                    if attempt < max_attempts:
                        time.sleep(wait)
                        wait *= backoff
            raise last_exc  # type: ignore[misc]
        return wrapper  # type: ignore[return-value]
    return decorator
