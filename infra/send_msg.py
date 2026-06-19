"""
钉钉消息推送模块
"""
from __future__ import annotations

import hashlib
import hmac
import base64
import time
import urllib.parse

import requests

from infra.env import DINGTALK_WEBHOOK, DINGTALK_SECRET


def _sign() -> str:
    """生成钉钉加签参数"""
    if not DINGTALK_SECRET:
        return ""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{DINGTALK_SECRET}"
    hmac_code = hmac.new(
        DINGTALK_SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"&timestamp={timestamp}&sign={sign}"


def send_dingtalk(msg: str = "") -> None:
    """向钉钉群机器人发送文本消息"""
    if not DINGTALK_WEBHOOK:
        return
    url = DINGTALK_WEBHOOK + _sign()
    payload = {
        "msgtype": "text",
        "text": {"content": msg},
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") != 0:
            print(f"send_dingtalk 钉钉返回错误: {result}")
    except requests.RequestException as e:
        print(f"send_dingtalk 异常: {e}")
