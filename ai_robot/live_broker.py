from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

from ai_robot.config import BotConfig
from ai_robot.logging_utils import append_jsonl, utc_now_iso, write_json
from ai_robot.okx_private import OKXPrivateClient
from ai_robot.okx_public import OKXPublicClient
from ai_robot.risk_limits import daily_loss_limit_usdt


LIVE_ENABLE_VALUE = "I_UNDERSTAND_REAL_MONEY_IS_AT_RISK"


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def quantize_down(value: float, step: float) -> str:
    if step <= 0:
        raise ValueError("step must be positive")
    places = max(0, -Decimal(str(step)).as_tuple().exponent)
    quantity = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step))
    return f"{quantity:.{places}f}"


def price_text(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def read_state_json(path: Any, default: dict[str, Any]) -> dict[str, Any]:
    try:
        if not path.exists():
            return dict(default)
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return dict(default)
        import json

        return json.loads(raw)
    except Exception:
        return dict(default)


class LiveBroker:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.client = OKXPrivateClient()
        self.public = OKXPublicClient()
        self.state_path = config.logs_dir / "live_state.json"
        self.daily_path = config.logs_dir / "live_daily_state.json"
        self.trades_path = config.logs_dir / "live_trades.jsonl"
        self.state = read_state_json(self.state_path, {"open_positions": [], "realized_pnl_usdt": 0.0})
        self._equity_usdt_cache: float | None = None
        self.daily = self._load_daily()
        self.refresh_daily_limits()
        self._persist()

    @property
    def live_enabled(self) -> bool:
        return os.environ.get("LIVE_TRADING_ENABLED", "") == LIVE_ENABLE_VALUE

    def _load_daily(self) -> dict[str, Any]:
        default = {
            "date": today_utc(),
            "daily_net_pnl_usdt": 0.0,
            "account_equity_usdt": self.account_equity_usdt(),
            "daily_net_loss_limit_usdt": self.config.daily_net_loss_limit_usdt,
            "daily_profit_stop_enabled": False,
            "trades_count": 0,
            "wins": 0,
            "losses": 0,
            "consecutive_losses": 0,
            "stop_trading_today": False,
            "stop_reason": None,
        }
        current = read_state_json(self.daily_path, default)
        if current.get("date") != today_utc():
            return default
        if current.get("stop_reason") == "daily_net_profit_target_reached":
            current["stop_trading_today"] = False
            current["stop_reason"] = None
        return current

    def account_equity_usdt(self) -> float:
        if self._equity_usdt_cache is not None:
            return self._equity_usdt_cache
        equity = float(self.state.get("account_equity_usdt", self.config.starting_balance_usdt))
        if self.client.available:
            try:
                data = self.client.balance("USDT").get("data", [])
                if data:
                    total_eq = data[0].get("totalEq")
                    if total_eq is not None:
                        equity = float(total_eq)
            except Exception:
                pass
        self._equity_usdt_cache = equity
        self.state["account_equity_usdt"] = equity
        return equity

    def refresh_daily_limits(self) -> None:
        equity = self.account_equity_usdt()
        self.daily["account_equity_usdt"] = equity
        self.daily["daily_net_loss_limit_usdt"] = daily_loss_limit_usdt(self.config, equity)
        self.daily["daily_profit_stop_enabled"] = False
        self.daily.pop("daily_net_profit_target_usdt", None)

    def daily_loss_limit_usdt(self) -> float:
        self.refresh_daily_limits()
        return float(self.daily["daily_net_loss_limit_usdt"])

    @property
    def open_positions(self) -> list[dict[str, Any]]:
        positions = self.state.get("open_positions")
        if not isinstance(positions, list):
            return []
        return [position for position in positions if isinstance(position, dict)]

    def open_symbols(self) -> set[str]:
        return {str(position.get("symbol")) for position in self.open_positions}

    def can_open(self, symbol: str | None = None) -> tuple[bool, str | None]:
        if not self.live_enabled:
            return False, "live_trading_not_enabled"
        if not self.client.available:
            return False, "missing_okx_credentials"
        if self.daily.get("stop_trading_today"):
            return False, str(self.daily.get("stop_reason") or "stop_trading_today")
        if len(self.open_positions) >= self.config.max_open_positions:
            return False, "max_open_positions_reached"
        if symbol and symbol in self.open_symbols():
            return False, "symbol_already_has_open_position"
        if self.daily["daily_net_pnl_usdt"] <= self.daily_loss_limit_usdt():
            self.stop_today("daily_net_loss_limit_reached")
            return False, "daily_net_loss_limit_reached"
        if self.daily["consecutive_losses"] >= self.config.max_consecutive_losses:
            self.stop_today("consecutive_losses_limit")
            return False, "consecutive_losses_limit"
        return True, None

    def contract_size(self, symbol: str, price: float, notional_usdt: float) -> str:
        instrument = self.public.instrument(symbol)
        ct_val = float(instrument.get("ctVal") or 0.0)
        lot_size = float(instrument.get("lotSz") or 0.0)
        if ct_val <= 0 or lot_size <= 0:
            raise RuntimeError(f"Invalid instrument metadata for {symbol}: {instrument}")
        contracts = notional_usdt / max(price * ct_val, 1e-12)
        contracts = max(contracts, lot_size)
        contracts = math.floor(contracts / lot_size) * lot_size
        return quantize_down(contracts, lot_size)

    def exchange_position_keys(self) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        if not self.client.available:
            return keys
        positions = self.client.positions("SWAP").get("data", [])
        for item in positions:
            try:
                pos = float(item.get("pos") or 0.0)
            except (TypeError, ValueError):
                pos = 0.0
            if pos == 0:
                continue
            symbol = str(item.get("instId") or "")
            pos_side = str(item.get("posSide") or "").lower()
            if symbol and pos_side:
                keys.add((symbol, pos_side))
        return keys

    def open(self, signal: dict[str, Any], price: float) -> dict[str, Any]:
        can_open, reason = self.can_open(str(signal["symbol"]))
        if not can_open:
            raise RuntimeError(f"Live open blocked: {reason}")
        side = "buy" if signal["side"] == "LONG" else "sell"
        pos_side = "long" if signal["side"] == "LONG" else "short"
        symbol = str(signal["symbol"])
        notional = min(float(signal["notional_usdt"]), self.config.max_notional_per_trade_usdt)
        size = self.contract_size(symbol, price, notional)
        if float(size) <= 0:
            raise RuntimeError("Live order size rounded to zero")
        self.client.set_leverage(symbol, self.config.leverage, self.config.margin_mode, pos_side=pos_side)
        response = self.client.place_market_order(
            inst_id=symbol,
            td_mode=self.config.margin_mode,
            side=side,
            size=size,
            client_order_id=signal["trade_id"].replace("-", "")[-32:],
            pos_side=pos_side,
            attach_algo_ords=[
                {
                    "attachAlgoClOrdId": f"tpsl{signal['trade_id']}".replace("-", "")[-32:],
                    "tpTriggerPx": price_text(float(signal["take_profit_price"])),
                    "tpOrdPx": "-1",
                    "tpTriggerPxType": "mark",
                    "slTriggerPx": price_text(float(signal["stop_loss_price"])),
                    "slOrdPx": "-1",
                    "slTriggerPxType": "mark",
                }
            ],
        )
        position = {
            "trade_id": signal["trade_id"],
            "timestamp": utc_now_iso(),
            "symbol": symbol,
            "side": signal["side"],
            "entry_price": price,
            "size": size,
            "notional_usdt": notional,
            "take_profit_price": signal["take_profit_price"],
            "stop_loss_price": signal["stop_loss_price"],
            "order_response": response,
        }
        positions = self.open_positions
        positions.append(position)
        self.state["open_positions"] = positions
        append_jsonl(self.trades_path, {"event": "OPEN", "mode": "live", "price": price, **position})
        self._persist()
        return position

    def maybe_close(self, mark_prices: dict[str, float]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        exchange_keys: set[tuple[str, str]] | None = None
        if self.client.available and self.open_positions:
            try:
                exchange_keys = self.exchange_position_keys()
            except Exception:
                exchange_keys = None
        for position in list(self.open_positions):
            symbol = str(position["symbol"])
            mark = mark_prices.get(symbol)
            if mark is None:
                continue
            pos_side = "long" if position["side"] == "LONG" else "short"
            if exchange_keys is not None and (symbol, pos_side) not in exchange_keys:
                events.append(self.record_exchange_closed(position["trade_id"], mark))
                continue
            reason = None
            if position["side"] == "LONG":
                if mark >= float(position["take_profit_price"]):
                    reason = "take_profit"
                elif mark <= float(position["stop_loss_price"]):
                    reason = "stop_loss"
            else:
                if mark <= float(position["take_profit_price"]):
                    reason = "take_profit"
                elif mark >= float(position["stop_loss_price"]):
                    reason = "stop_loss"
            if reason is not None:
                events.append(self.close(position["trade_id"], mark, reason))
        return events

    def estimated_net_pnl(self, position: dict[str, Any], price: float) -> float:
        if position["side"] == "LONG":
            gross_pnl = (price - float(position["entry_price"])) / float(position["entry_price"]) * float(position["notional_usdt"])
        else:
            gross_pnl = (float(position["entry_price"]) - price) / float(position["entry_price"]) * float(position["notional_usdt"])
        estimated_fees = float(position["notional_usdt"]) * (self.config.taker_fee_rate * 2)
        return gross_pnl - estimated_fees

    def apply_close_accounting(self, position: dict[str, Any], net_pnl: float) -> None:
        self.state["open_positions"] = [item for item in self.open_positions if item.get("trade_id") != position["trade_id"]]
        self.state["realized_pnl_usdt"] = float(self.state.get("realized_pnl_usdt", 0.0)) + net_pnl
        self.daily["daily_net_pnl_usdt"] += net_pnl
        self._equity_usdt_cache = None
        self.daily["trades_count"] += 1
        if net_pnl > 0:
            self.daily["wins"] += 1
            self.daily["consecutive_losses"] = 0
        else:
            self.daily["losses"] += 1
            self.daily["consecutive_losses"] += 1
        if self.daily["daily_net_pnl_usdt"] <= self.daily_loss_limit_usdt():
            self.stop_today("daily_net_loss_limit_reached")
        elif self.daily["consecutive_losses"] >= self.config.max_consecutive_losses:
            self.stop_today("consecutive_losses_limit")

    def record_exchange_closed(self, trade_id: str, price: float) -> dict[str, Any]:
        position = next((item for item in self.open_positions if item.get("trade_id") == trade_id), None)
        if position is None:
            raise RuntimeError(f"No live position to record closed: {trade_id}")
        net_pnl = self.estimated_net_pnl(position, price)
        self.apply_close_accounting(position, net_pnl)
        event = {
            "event": "CLOSE",
            "mode": "live",
            "timestamp": utc_now_iso(),
            "trade_id": trade_id,
            "symbol": position["symbol"],
            "side": "SELL" if position["side"] == "LONG" else "BUY",
            "price": price,
            "size": position["size"],
            "estimated_net_pnl_usdt": net_pnl,
            "exit_reason": "exchange_tpsl_closed",
        }
        append_jsonl(self.trades_path, event)
        self._persist()
        return event

    def close(self, trade_id: str, price: float, exit_reason: str) -> dict[str, Any]:
        position = next((item for item in self.open_positions if item.get("trade_id") == trade_id), None)
        if position is None:
            raise RuntimeError(f"No live position to close: {trade_id}")
        side = "sell" if position["side"] == "LONG" else "buy"
        pos_side = "long" if position["side"] == "LONG" else "short"
        response = self.client.place_market_order(
            inst_id=str(position["symbol"]),
            td_mode=self.config.margin_mode,
            side=side,
            size=str(position["size"]),
            client_order_id=f"close{trade_id}".replace("-", "")[-32:],
            reduce_only=True,
            pos_side=pos_side,
        )
        net_pnl = self.estimated_net_pnl(position, price)
        self.apply_close_accounting(position, net_pnl)
        event = {
            "event": "CLOSE",
            "mode": "live",
            "timestamp": utc_now_iso(),
            "trade_id": trade_id,
            "symbol": position["symbol"],
            "side": side.upper(),
            "price": price,
            "size": position["size"],
            "estimated_net_pnl_usdt": net_pnl,
            "exit_reason": exit_reason,
            "order_response": response,
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
