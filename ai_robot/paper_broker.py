from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ai_robot.config import BotConfig
from ai_robot.logging_utils import append_jsonl, read_json, utc_now_iso, write_json
from ai_robot.risk_limits import daily_loss_limit_usdt


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class PaperBroker:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.state_path = config.logs_dir / "paper_state.json"
        self.daily_path = config.logs_dir / "daily_state.json"
        self.trades_path = config.logs_dir / "trades.jsonl"
        self.state = read_json(
            self.state_path,
            {
                "equity_usdt": config.starting_balance_usdt,
                "realized_pnl_usdt": 0.0,
                "open_positions": [],
            },
        )
        self._migrate_state()
        self.daily = self._load_daily()
        self._persist()

    def _migrate_state(self) -> None:
        legacy_position = self.state.get("open_position")
        if "open_positions" not in self.state:
            self.state["open_positions"] = []
        if isinstance(legacy_position, dict):
            existing_ids = {position.get("trade_id") for position in self.state["open_positions"]}
            if legacy_position.get("trade_id") not in existing_ids:
                self.state["open_positions"].append(legacy_position)
        self.state["open_position"] = None

    def _load_daily(self) -> dict[str, Any]:
        default = {
            "date": today_utc(),
            "starting_equity_usdt": float(self.state.get("equity_usdt", self.config.starting_balance_usdt)),
            "current_equity_usdt": float(self.state.get("equity_usdt", self.config.starting_balance_usdt)),
            "daily_gross_pnl_usdt": 0.0,
            "daily_fees_usdt": 0.0,
            "daily_funding_usdt": 0.0,
            "daily_net_pnl_usdt": 0.0,
            "daily_net_loss_limit_usdt": self.daily_loss_limit_usdt(),
            "daily_profit_stop_enabled": False,
            "trades_count": 0,
            "wins": 0,
            "losses": 0,
            "consecutive_losses": 0,
            "stop_trading_today": False,
            "stop_reason": None,
        }
        current = read_json(self.daily_path, default)
        if current.get("date") != today_utc():
            return default
        if current.get("stop_reason") == "daily_net_profit_target_reached":
            current["stop_trading_today"] = False
            current["stop_reason"] = None
        if self.config.max_consecutive_losses <= 0 and current.get("stop_reason") == "consecutive_losses_limit":
            current["stop_trading_today"] = False
            current["stop_reason"] = None
        current["daily_net_loss_limit_usdt"] = self.daily_loss_limit_usdt()
        current["daily_profit_stop_enabled"] = False
        current.pop("daily_net_profit_target_usdt", None)
        return current

    def current_equity_usdt(self) -> float:
        return float(self.state.get("equity_usdt", self.config.starting_balance_usdt))

    def daily_loss_limit_usdt(self) -> float:
        return daily_loss_limit_usdt(self.config, self.current_equity_usdt())

    @property
    def open_position(self) -> dict[str, Any] | None:
        positions = self.open_positions
        return positions[0] if positions else None

    @property
    def open_positions(self) -> list[dict[str, Any]]:
        positions = self.state.get("open_positions")
        if not isinstance(positions, list):
            return []
        return [position for position in positions if isinstance(position, dict)]

    def open_symbols(self) -> set[str]:
        return {str(position.get("symbol")) for position in self.open_positions}

    def can_open(self, symbol: str | None = None) -> tuple[bool, str | None]:
        if self.daily.get("stop_trading_today"):
            return False, str(self.daily.get("stop_reason") or "stop_trading_today")
        if len(self.open_positions) >= self.config.max_open_positions:
            return False, "max_open_positions_reached"
        if symbol and symbol in self.open_symbols():
            return False, "symbol_already_has_open_position"
        self.daily["daily_net_loss_limit_usdt"] = self.daily_loss_limit_usdt()
        self.daily["daily_profit_stop_enabled"] = False
        if self.daily["daily_net_pnl_usdt"] <= self.daily["daily_net_loss_limit_usdt"]:
            self.stop_today("daily_net_loss_limit_reached")
            return False, "daily_net_loss_limit_reached"
        if self.config.max_consecutive_losses > 0 and self.daily["consecutive_losses"] >= self.config.max_consecutive_losses:
            self.stop_today("consecutive_losses_limit")
            return False, "consecutive_losses_limit"
        return True, None

    def open(self, signal: dict[str, Any], price: float) -> dict[str, Any]:
        fee = signal["notional_usdt"] * signal["entry_fee_rate"]
        quantity = signal["notional_usdt"] / price
        position = {
            "trade_id": signal["trade_id"],
            "timestamp": utc_now_iso(),
            "symbol": signal["symbol"],
            "side": signal["side"],
            "entry_price": price,
            "quantity": quantity,
            "notional_usdt": signal["notional_usdt"],
            "margin_usdt": signal["margin_usdt"],
            "leverage": signal["leverage"],
            "take_profit_price": signal["take_profit_price"],
            "stop_loss_price": signal["stop_loss_price"],
            "entry_fee_usdt": fee,
            "entry_fee_type": signal["entry_fee_type"],
            "exit_fee_type": signal["exit_fee_type"],
            "ai_confidence": signal["ai_confidence"],
        }
        positions = self.open_positions
        positions.append(position)
        self.state["open_positions"] = positions
        self.state["open_position"] = None
        self.state["equity_usdt"] = float(self.state.get("equity_usdt", self.config.starting_balance_usdt)) - fee
        self.daily["daily_fees_usdt"] += fee
        self.daily["daily_net_pnl_usdt"] -= fee
        self.daily["current_equity_usdt"] = self.state["equity_usdt"]
        append_jsonl(
            self.trades_path,
            {
                "event": "OPEN",
                "mode": self.config.mode,
                "price": price,
                "fee_usdt": fee,
                **position,
            },
        )
        self._persist()
        return position

    def maybe_close(self, mark_prices: dict[str, float]) -> list[dict[str, Any]]:
        close_events: list[dict[str, Any]] = []
        for position in list(self.open_positions):
            symbol = position["symbol"]
            mark = mark_prices.get(symbol)
            if mark is None:
                continue

            side = position["side"]
            exit_reason = None
            if side == "LONG":
                if mark >= position["take_profit_price"]:
                    exit_reason = "take_profit"
                elif mark <= position["stop_loss_price"]:
                    exit_reason = "stop_loss"
            else:
                if mark <= position["take_profit_price"]:
                    exit_reason = "take_profit"
                elif mark >= position["stop_loss_price"]:
                    exit_reason = "stop_loss"
            if exit_reason is not None:
                close_events.append(self.close(position["trade_id"], mark, exit_reason))
        return close_events

    def close(self, trade_id: str, price: float, exit_reason: str) -> dict[str, Any]:
        position = next((item for item in self.open_positions if item.get("trade_id") == trade_id), None)
        if position is None:
            raise RuntimeError(f"No open position to close: {trade_id}")

        quantity = float(position["quantity"])
        notional = quantity * price
        if position["side"] == "LONG":
            gross_pnl = (price - position["entry_price"]) * quantity
        else:
            gross_pnl = (position["entry_price"] - price) * quantity
        exit_fee_rate = self.config.maker_fee_rate if position["exit_fee_type"] == "maker" else self.config.taker_fee_rate
        exit_fee = notional * exit_fee_rate
        net_pnl = gross_pnl - float(position["entry_fee_usdt"]) - exit_fee

        self.state["open_positions"] = [item for item in self.open_positions if item.get("trade_id") != trade_id]
        self.state["open_position"] = None
        self.state["realized_pnl_usdt"] = float(self.state.get("realized_pnl_usdt", 0.0)) + net_pnl
        self.state["equity_usdt"] = float(self.state.get("equity_usdt", self.config.starting_balance_usdt)) + gross_pnl - exit_fee
        self.daily["daily_gross_pnl_usdt"] += gross_pnl
        self.daily["daily_fees_usdt"] += exit_fee
        self.daily["daily_net_pnl_usdt"] += gross_pnl - exit_fee
        self.daily["current_equity_usdt"] = self.state["equity_usdt"]
        self.daily["trades_count"] += 1
        if net_pnl > 0:
            self.daily["wins"] += 1
            self.daily["consecutive_losses"] = 0
        else:
            self.daily["losses"] += 1
            self.daily["consecutive_losses"] += 1

        self.daily["daily_net_loss_limit_usdt"] = self.daily_loss_limit_usdt()
        self.daily["daily_profit_stop_enabled"] = False
        if self.daily["daily_net_pnl_usdt"] <= self.daily["daily_net_loss_limit_usdt"]:
            self.stop_today("daily_net_loss_limit_reached")
        elif self.config.max_consecutive_losses > 0 and self.daily["consecutive_losses"] >= self.config.max_consecutive_losses:
            self.stop_today("consecutive_losses_limit")

        event = {
            "event": "CLOSE",
            "mode": self.config.mode,
            "timestamp": utc_now_iso(),
            "trade_id": position["trade_id"],
            "symbol": position["symbol"],
            "side": "SELL" if position["side"] == "LONG" else "BUY",
            "price": price,
            "quantity": quantity,
            "notional_usdt": notional,
            "fee_usdt": exit_fee,
            "gross_pnl_usdt": gross_pnl,
            "net_pnl_usdt": net_pnl,
            "exit_reason": exit_reason,
        }
        append_jsonl(self.trades_path, event)
        self._persist()
        return event

    def stop_today(self, reason: str) -> None:
        self.daily["stop_trading_today"] = True
        self.daily["stop_reason"] = reason
        self._persist()

    def _persist(self) -> None:
        write_json(self.state_path, self.state)
        write_json(self.daily_path, self.daily)
