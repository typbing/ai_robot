from __future__ import annotations

from ai_robot.config import BotConfig


def daily_loss_limit_usdt(config: BotConfig, equity_usdt: float) -> float:
    if config.daily_net_loss_limit_pct is not None:
        return min(0.0, equity_usdt * config.daily_net_loss_limit_pct)
    return config.daily_net_loss_limit_usdt
