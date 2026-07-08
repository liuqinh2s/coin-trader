"""
交易所业务异常
"""


class ExchangeBusinessError(Exception):
    """交易所返回的业务错误码（非网络异常）。

    区别于网络层 OSError/HTTPError，此异常表示请求本身合法但
    交易所拒绝了操作（如22002: 暂无仓位可平），不应重试。
    """

    def __init__(self, code: str, msg: str = "") -> None:
        self.code = code
        self.msg = msg
        super().__init__(f"[{code}] {msg}")
