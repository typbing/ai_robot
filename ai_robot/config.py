from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DeepSeekConfig:
    enabled: bool
    api_key_env: str
    base_url: str
    model: str
    timeout_seconds: int


@dataclass(frozen=True)
class BotConfig:
    mode: str
    exchange: str
    instrument_type: str
    symbols: list[str]
    base_timeframe: str
    trend_timeframe: str
    scan_interval_seconds: int
    local_exit_check_interval_seconds: int
    logs_dir: Path
    starting_balance_usdt: float
    daily_net_loss_limit_usdt: float
    daily_net_loss_limit_pct: float | None
    max_consecutive_losses: int
    margin_mode: str
    leverage: int
    max_margin_per_trade_usdt: float
    max_notional_per_trade_usdt: float
    max_open_positions: int
    maker_fee_rate: float
    taker_fee_rate: float
    entry_fee_type: str
    exit_fee_type: str
    funding_buffer_usdt: float
    target_net_profit_per_trade_usdt: float
    stop_loss_pct: float
    min_ai_confidence: float
    allow_long: bool
    allow_short: bool
    high_volatility_atr_pct: float
    min_trade_atr_pct: float
    trend_min_ema_gap_pct: float
    trend_max_extension_pct: float
    trend_momentum_max_extension_pct: float
    range_reversal_rsi_low: float
    range_reversal_rsi_high: float
    stale_data_seconds: int
    avoid_funding_minutes_before: int
    avoid_funding_minutes_after: int
    deepseek: DeepSeekConfig


def load_config(path: str | Path) -> BotConfig:
    config_path = Path(path)
    raw: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
    deepseek = DeepSeekConfig(**raw["deepseek"])
    logs_dir = Path(raw["logs_dir"])
    if not logs_dir.is_absolute():
        logs_dir = config_path.parent / logs_dir
    return BotConfig(
        mode=raw["mode"],
        exchange=raw["exchange"],
        instrument_type=raw["instrument_type"],
        symbols=list(raw["symbols"]),
        base_timeframe=raw["base_timeframe"],
        trend_timeframe=raw["trend_timeframe"],
        scan_interval_seconds=int(raw["scan_interval_seconds"]),
        local_exit_check_interval_seconds=int(
            raw.get("local_exit_check_interval_seconds", raw["scan_interval_seconds"])
        ),
        logs_dir=logs_dir,
        starting_balance_usdt=float(raw["starting_balance_usdt"]),
        daily_net_loss_limit_usdt=float(raw["daily_net_loss_limit_usdt"]),
        daily_net_loss_limit_pct=(
            float(raw["daily_net_loss_limit_pct"]) if raw.get("daily_net_loss_limit_pct") is not None else None
        ),
        max_consecutive_losses=int(raw["max_consecutive_losses"]),
        margin_mode=raw["margin_mode"],
        leverage=int(raw["leverage"]),
        max_margin_per_trade_usdt=float(raw["max_margin_per_trade_usdt"]),
        max_notional_per_trade_usdt=float(raw["max_notional_per_trade_usdt"]),
        max_open_positions=int(raw["max_open_positions"]),
        maker_fee_rate=float(raw["maker_fee_rate"]),
        taker_fee_rate=float(raw["taker_fee_rate"]),
        entry_fee_type=raw["entry_fee_type"],
        exit_fee_type=raw["exit_fee_type"],
        funding_buffer_usdt=float(raw["funding_buffer_usdt"]),
        target_net_profit_per_trade_usdt=float(raw["target_net_profit_per_trade_usdt"]),
        stop_loss_pct=float(raw["stop_loss_pct"]),
        min_ai_confidence=float(raw["min_ai_confidence"]),
        allow_long=bool(raw["allow_long"]),
        allow_short=bool(raw["allow_short"]),
        high_volatility_atr_pct=float(raw["high_volatility_atr_pct"]),
        min_trade_atr_pct=float(raw.get("min_trade_atr_pct", 0.0)),
        trend_min_ema_gap_pct=float(raw.get("trend_min_ema_gap_pct", 0.0)),
        trend_max_extension_pct=float(raw.get("trend_max_extension_pct", 1.0)),
        trend_momentum_max_extension_pct=float(
            raw.get("trend_momentum_max_extension_pct", raw.get("trend_max_extension_pct", 1.0))
        ),
        range_reversal_rsi_low=float(raw.get("range_reversal_rsi_low", 35.0)),
        range_reversal_rsi_high=float(raw.get("range_reversal_rsi_high", 65.0)),
        stale_data_seconds=int(raw["stale_data_seconds"]),
        avoid_funding_minutes_before=int(raw["avoid_funding_minutes_before"]),
        avoid_funding_minutes_after=int(raw["avoid_funding_minutes_after"]),
        deepseek=deepseek,
    )
