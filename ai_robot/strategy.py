from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ai_robot.config import BotConfig


def fee_rate(config: BotConfig, fee_type: str) -> float:
    if fee_type == "maker":
        return config.maker_fee_rate
    if fee_type == "taker":
        return config.taker_fee_rate
    raise ValueError(f"Unsupported fee type: {fee_type}")


def near_funding_window(config: BotConfig, seconds_until_funding: float | None) -> bool:
    if seconds_until_funding is None:
        return False
    minutes = seconds_until_funding / 60
    return -config.avoid_funding_minutes_after <= minutes <= config.avoid_funding_minutes_before


def fee_adjusted_trade_shape(config: BotConfig) -> dict[str, float | str]:
    margin_usdt = min(config.max_margin_per_trade_usdt, config.max_notional_per_trade_usdt / config.leverage)
    notional_usdt = min(margin_usdt * config.leverage, config.max_notional_per_trade_usdt)
    entry_rate = fee_rate(config, config.entry_fee_type)
    exit_rate = fee_rate(config, config.exit_fee_type)
    estimated_entry_fee = notional_usdt * entry_rate
    estimated_exit_fee = notional_usdt * exit_rate
    estimated_total_fee = estimated_entry_fee + estimated_exit_fee + config.funding_buffer_usdt
    target_gross_pnl = config.target_net_profit_per_trade_usdt + estimated_total_fee
    take_profit_pct = target_gross_pnl / notional_usdt
    max_gross_loss = notional_usdt * config.stop_loss_pct
    max_net_loss = max_gross_loss + estimated_entry_fee + estimated_exit_fee
    return {
        "margin_usdt": margin_usdt,
        "notional_usdt": notional_usdt,
        "entry_rate": entry_rate,
        "exit_rate": exit_rate,
        "estimated_entry_fee": estimated_entry_fee,
        "estimated_exit_fee": estimated_exit_fee,
        "estimated_total_fee": estimated_total_fee,
        "target_gross_pnl": target_gross_pnl,
        "take_profit_pct": take_profit_pct,
        "max_gross_loss": max_gross_loss,
        "max_net_loss": max_net_loss,
    }


def rule_prefilter(
    config: BotConfig,
    symbol: str,
    market: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if market["market_regime_rule"] == "high_volatility":
        return None, "high_volatility"
    if float(market.get("atr_pct", 0.0)) < config.min_trade_atr_pct:
        return None, "volatility_too_low"

    shape = fee_adjusted_trade_shape(config)
    if float(shape["target_gross_pnl"]) <= float(shape["estimated_total_fee"]):
        return None, "fee_adjusted_profit_too_small"

    regime = str(market["market_regime_rule"])
    rsi_value = float(market["rsi_base"])
    ema_gap = float(market.get("ema_gap_trend_pct", 0.0))
    extension = float(market.get("price_ema20_distance_pct", 0.0))
    last_change = float(market.get("last_close_change_pct", 0.0))
    up_closes = int(float(market.get("consecutive_up_closes", 0)))
    down_closes = int(float(market.get("consecutive_down_closes", 0)))
    volume = str(market.get("volume_state", "unknown"))
    action = "HOLD"
    reason = "No rule candidate."

    trend_quality_ok = ema_gap >= config.trend_min_ema_gap_pct and extension <= config.trend_max_extension_pct
    range_volume_ok = volume != "high"
    range_long_confirmed = last_change > 0 and down_closes < 3
    range_short_confirmed = last_change < 0 and up_closes < 3

    if regime == "bullish_trend" and trend_quality_ok and 42 <= rsi_value <= 64:
        action = "LONG"
        reason = "Rule candidate: bullish trend, sufficient EMA separation, controlled extension, and pullback RSI."
    elif regime == "bearish_trend" and trend_quality_ok and 36 <= rsi_value <= 58:
        action = "SHORT"
        reason = "Rule candidate: bearish trend, sufficient EMA separation, controlled extension, and rebound RSI."
    elif regime == "range" and rsi_value <= config.range_reversal_rsi_low and range_volume_ok and range_long_confirmed:
        action = "LONG"
        reason = "Rule candidate: range oversold RSI with close-price rebound confirmation and no high-volume breakdown."
    elif regime == "range" and rsi_value >= config.range_reversal_rsi_high and range_volume_ok and range_short_confirmed:
        action = "SHORT"
        reason = "Rule candidate: range overbought RSI with close-price pullback confirmation and no high-volume breakout."

    if action == "LONG" and not config.allow_long:
        return None, "long_not_allowed"
    if action == "SHORT" and not config.allow_short:
        return None, "short_not_allowed"
    if action == "HOLD":
        return None, "no_rule_candidate"

    return {
        "symbol": symbol,
        "preferred_action": action,
        "market_regime": regime,
        "reason": reason,
        "source": "rule_prefilter",
        "trade_shape": shape,
    }, None


def build_signal(
    config: BotConfig,
    symbol: str,
    market: dict[str, Any],
    ai: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if config.mode not in {"paper", "live"}:
        return None, "unsupported_mode"

    if not ai.get("allow_trade"):
        return None, "ai_rejected"

    confidence = float(ai.get("confidence", 0.0))
    if confidence < config.min_ai_confidence:
        return None, "ai_confidence_too_low"

    side = str(ai.get("preferred_action", "HOLD")).upper()
    if side == "LONG" and not config.allow_long:
        return None, "long_not_allowed"
    if side == "SHORT" and not config.allow_short:
        return None, "short_not_allowed"
    if side not in {"LONG", "SHORT"}:
        return None, "hold_action"

    if market["market_regime_rule"] == "high_volatility":
        return None, "high_volatility"

    price = float(market["price"])
    shape = fee_adjusted_trade_shape(config)
    margin_usdt = float(shape["margin_usdt"])
    notional_usdt = float(shape["notional_usdt"])
    entry_rate = float(shape["entry_rate"])
    exit_rate = float(shape["exit_rate"])
    estimated_entry_fee = float(shape["estimated_entry_fee"])
    estimated_exit_fee = float(shape["estimated_exit_fee"])
    estimated_total_fee = float(shape["estimated_total_fee"])
    target_gross_pnl = float(shape["target_gross_pnl"])
    take_profit_pct = float(shape["take_profit_pct"])
    max_gross_loss = float(shape["max_gross_loss"])
    max_net_loss = float(shape["max_net_loss"])

    if target_gross_pnl <= estimated_total_fee:
        return None, "fee_adjusted_profit_too_small"

    if side == "LONG":
        take_profit_price = price * (1 + take_profit_pct)
        stop_loss_price = price * (1 - config.stop_loss_pct)
    else:
        take_profit_price = price * (1 - take_profit_pct)
        stop_loss_price = price * (1 + config.stop_loss_pct)

    trade_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{symbol}-{side}"
    signal = {
        "trade_id": trade_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": config.mode,
        "exchange": config.exchange,
        "symbol": symbol,
        "instrument_type": config.instrument_type,
        "side": side,
        "margin_mode": config.margin_mode,
        "leverage": config.leverage,
        "margin_usdt": round(margin_usdt, 8),
        "notional_usdt": round(notional_usdt, 8),
        "entry_price": price,
        "take_profit_pct": take_profit_pct,
        "stop_loss_pct": config.stop_loss_pct,
        "take_profit_price": take_profit_price,
        "stop_loss_price": stop_loss_price,
        "maker_fee_rate": config.maker_fee_rate,
        "taker_fee_rate": config.taker_fee_rate,
        "entry_fee_type": config.entry_fee_type,
        "exit_fee_type": config.exit_fee_type,
        "entry_fee_rate": entry_rate,
        "exit_fee_rate": exit_rate,
        "estimated_entry_fee_usdt": estimated_entry_fee,
        "estimated_exit_fee_usdt": estimated_exit_fee,
        "estimated_funding_buffer_usdt": config.funding_buffer_usdt,
        "estimated_total_cost_usdt": estimated_total_fee,
        "target_gross_pnl_usdt": target_gross_pnl,
        "target_net_pnl_usdt": config.target_net_profit_per_trade_usdt,
        "max_gross_loss_usdt": max_gross_loss,
        "max_net_loss_usdt": max_net_loss,
        "market_regime": ai.get("market_regime"),
        "rule_market_regime": market.get("market_regime_rule"),
        "ai_confidence": confidence,
        "ai_source": ai.get("source", "unknown"),
        "reason": ai.get("reason", ""),
    }
    return signal, None
