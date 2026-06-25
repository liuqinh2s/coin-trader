"""Helpers for isolated-margin liquidation protection."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ExtraMarginPlan:
    extra_margin: float
    required_margin: float
    initial_margin: float
    target_liquidation_price: float
    capped: bool = False


def estimate_extra_isolated_margin(
    entry_price: float,
    size: float,
    stop_price: float,
    leverage: int | float,
    equity: float,
    auto_cfg: dict,
) -> ExtraMarginPlan:
    """Estimate extra isolated margin needed to keep liquidation below stop."""
    if entry_price <= 0 or size <= 0 or stop_price <= 0 or leverage <= 0:
        return ExtraMarginPlan(0.0, 0.0, 0.0, 0.0)

    stop_buffer = float(auto_cfg.get("liquidation_stop_buffer_pct", 0.005))
    margin_buffer = float(auto_cfg.get("liquidation_margin_buffer_pct", 0.004))
    max_extra_margin_pct = float(auto_cfg.get("max_extra_margin_pct", 0.08))

    target_liq = stop_price * (1 - stop_buffer)
    if not math.isfinite(target_liq) or target_liq <= 0 or target_liq >= entry_price:
        return ExtraMarginPlan(0.0, 0.0, 0.0, target_liq)

    notional = entry_price * size
    initial_margin = notional / leverage
    required_margin = (entry_price - target_liq) * size + notional * margin_buffer
    extra_margin = max(required_margin - initial_margin, 0.0)

    max_extra_margin = equity * max_extra_margin_pct if equity > 0 else 0.0
    capped = max_extra_margin > 0 and extra_margin > max_extra_margin
    if capped:
        extra_margin = max_extra_margin

    return ExtraMarginPlan(
        extra_margin=extra_margin,
        required_margin=required_margin,
        initial_margin=initial_margin,
        target_liquidation_price=target_liq,
        capped=capped,
    )
