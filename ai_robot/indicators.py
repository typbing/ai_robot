from __future__ import annotations

from statistics import mean

from ai_robot.okx_public import Candle


def ema(values: list[float], period: int) -> float:
    if not values:
        raise ValueError("ema requires values")
    k = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * k + result * (1 - k)
    return result


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains: list[float] = []
    losses: list[float] = []
    for prev, current in zip(values[-period - 1 : -1], values[-period:]):
        change = current - prev
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    avg_gain = mean(gains) if gains else 0.0
    avg_loss = mean(losses) if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) <= period:
        return 0.0
    true_ranges: list[float] = []
    recent = candles[-period:]
    previous_close = candles[-period - 1].close
    for candle in recent:
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
        previous_close = candle.close
    return mean(true_ranges)


def volume_state(candles: list[Candle], period: int = 20) -> str:
    if len(candles) < period + 1:
        return "unknown"
    avg_volume = mean([c.volume for c in candles[-period - 1 : -1]])
    current = candles[-1].volume
    if avg_volume <= 0:
        return "unknown"
    ratio = current / avg_volume
    if ratio >= 1.5:
        return "high"
    if ratio <= 0.5:
        return "low"
    return "normal"


def consecutive_direction(values: list[float], direction: str, max_bars: int = 5) -> int:
    if len(values) < 2:
        return 0
    count = 0
    for prev, current in zip(reversed(values[:-1]), reversed(values[1:])):
        if direction == "up" and current > prev:
            count += 1
        elif direction == "down" and current < prev:
            count += 1
        else:
            break
        if count >= max_bars:
            break
    return count


def summarize_market(base: list[Candle], trend: list[Candle], high_volatility_atr_pct: float) -> dict[str, float | str]:
    base_closes = [c.close for c in base]
    trend_closes = [c.close for c in trend]
    last_price = base[-1].close
    ema20_base = ema(base_closes[-60:], 20)
    ema50_base = ema(base_closes[-80:], 50)
    ema20_trend = ema(trend_closes[-60:], 20)
    ema50_trend = ema(trend_closes[-80:], 50)
    atr_value = atr(base, 14)
    atr_pct = atr_value / last_price if last_price else 0.0
    current_rsi = rsi(base_closes, 14)
    ema_gap_trend_pct = abs(ema20_trend - ema50_trend) / last_price if last_price else 0.0
    price_ema20_distance_pct = abs(last_price - ema20_base) / last_price if last_price else 0.0
    last_close_change_pct = (
        (base_closes[-1] - base_closes[-2]) / base_closes[-2]
        if len(base_closes) >= 2 and base_closes[-2]
        else 0.0
    )

    if atr_pct >= high_volatility_atr_pct:
        regime = "high_volatility"
    elif ema20_trend > ema50_trend and ema20_base > ema50_base and last_price > ema20_base:
        regime = "bullish_trend"
    elif ema20_trend < ema50_trend and ema20_base < ema50_base and last_price < ema20_base:
        regime = "bearish_trend"
    else:
        regime = "range"

    if ema20_trend > ema50_trend:
        trend_timeframe_bias = "bullish"
    elif ema20_trend < ema50_trend:
        trend_timeframe_bias = "bearish"
    else:
        trend_timeframe_bias = "flat"

    if ema20_base > ema50_base and last_price > ema20_base:
        base_timeframe_bias = "bullish"
    elif ema20_base < ema50_base and last_price < ema20_base:
        base_timeframe_bias = "bearish"
    else:
        base_timeframe_bias = "mixed"

    return {
        "price": last_price,
        "ema20_base": ema20_base,
        "ema50_base": ema50_base,
        "ema20_trend": ema20_trend,
        "ema50_trend": ema50_trend,
        "rsi_base": current_rsi,
        "atr": atr_value,
        "atr_pct": atr_pct,
        "ema_gap_trend_pct": ema_gap_trend_pct,
        "price_ema20_distance_pct": price_ema20_distance_pct,
        "last_close_change_pct": last_close_change_pct,
        "consecutive_up_closes": consecutive_direction(base_closes, "up"),
        "consecutive_down_closes": consecutive_direction(base_closes, "down"),
        "volume_state": volume_state(base),
        "market_regime_rule": regime,
        "trend_timeframe_bias": trend_timeframe_bias,
        "base_timeframe_bias": base_timeframe_bias,
        "regime_alignment": "aligned" if trend_timeframe_bias == base_timeframe_bias else "mixed",
        "last_candle_time": base[-1].timestamp.isoformat(),
    }
