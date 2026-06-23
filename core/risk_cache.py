"""Persistent risk cache for positions opened by this bot."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from infra.logger import log

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "runtime" / "cache"
POSITION_RISK_CACHE = CACHE_DIR / "position_risk.json"


def load_position_risk(path: Path = POSITION_RISK_CACHE) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("读取持仓风险缓存失败: %s", exc)
        return {}


def save_position_risk(cache: dict[str, Any], path: Path = POSITION_RISK_CACHE) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def record_position_risk(symbol: str, info: dict[str, Any]) -> None:
    cache = load_position_risk()
    cache[symbol] = {**info, "updated_at": int(time.time())}
    save_position_risk(cache)


def remove_position_risk(symbol: str) -> None:
    cache = load_position_risk()
    if symbol in cache:
        cache.pop(symbol, None)
        save_position_risk(cache)
