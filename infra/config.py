"""
配置管理：从 config.yaml 加载交易参数，支持运行时热加载
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_config: dict[str, Any] = {}


def _load() -> dict[str, Any]:
    """从 YAML 文件加载配置"""
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_config() -> dict[str, Any]:
    """获取配置（首次调用时加载）"""
    global _config
    if not _config:
        _config = _load()
    return _config


def reload_config() -> dict[str, Any]:
    """热加载配置"""
    global _config
    _config = _load()
    return _config
