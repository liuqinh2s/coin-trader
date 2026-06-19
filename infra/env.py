"""
环境配置：代理设置与敏感信息

注意：所有敏感信息（API Key、Secret、钉钉 Webhook 等）
     应通过环境变量注入，切勿硬编码在代码中。
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ---- 交易所选择 ----
# 支持: "bitget" (默认) / "binance"
EXCHANGE: str = os.getenv("EXCHANGE", "bitget").lower()

# ---- 代理配置 ----
NEED_PROXY: bool = os.getenv("NEED_PROXY",
                             os.getenv("BITGET_NEED_PROXY", "false")
                             ).lower() == "true"

_PROXY_HOST = os.getenv("PROXY_HOST", "127.0.0.1")
_PROXY_PORT = os.getenv("PROXY_PORT", "7890")

PROXIES = {
    "http": f"http://{_PROXY_HOST}:{_PROXY_PORT}",
    "https": f"http://{_PROXY_HOST}:{_PROXY_PORT}",
}

# ---- Bitget API 配置 ----
API_KEY: str = os.getenv("BITGET_API_KEY", "")
API_SECRET: str = os.getenv("BITGET_API_SECRET", "")
API_PASSPHRASE: str = os.getenv("BITGET_API_PASSPHRASE", "")

# ---- Bitget 模拟盘 ----
# 设为 true 启用模拟盘（需使用模拟盘 API Key）
BITGET_DEMO: bool = os.getenv("BITGET_DEMO", "false").lower() == "true"

# ---- Binance API 配置 ----
BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")

# ---- 钉钉配置 ----
# Webhook 地址（从钉钉群机器人设置中获取）
DINGTALK_WEBHOOK: str = os.getenv("DINGTALK_WEBHOOK", "")
# 加签密钥（可选，如果机器人开启了加签安全设置）
DINGTALK_SECRET: str = os.getenv("DINGTALK_SECRET", "")
