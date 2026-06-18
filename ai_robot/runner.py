from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import Any

from ai_robot.config import BotConfig, load_config
from ai_robot.deepseek_client import DeepSeekClient
from ai_robot.indicators import summarize_market
from ai_robot.logging_utils import append_jsonl, ensure_dir, utc_now_iso
from ai_robot.notifications import BarkNotifier, build_daily_summary, maybe_send_daily_summary
from ai_robot.okx_public import OKXPublicClient
from ai_robot.paper_broker import PaperBroker
from ai_robot.strategy import build_signal, near_funding_window, rule_prefilter


def build_account_summary(broker: PaperBroker, config: BotConfig) -> dict[str, Any]:
    return {
        "equity_usdt": broker.state["equity_usdt"],
        "daily_net_pnl_usdt": broker.daily["daily_net_pnl_usdt"],
        "daily_loss_limit_usdt": broker.daily_loss_limit_usdt(),
        "daily_profit_stop_enabled": False,
        "open_positions": len(broker.open_positions),
        "open_symbols": sorted(broker.open_symbols()),
        "max_open_positions": config.max_open_positions,
        "stop_trading_today": broker.daily["stop_trading_today"],
    }


def reject(config: BotConfig, payload: dict[str, Any]) -> None:
    append_jsonl(config.logs_dir / "rejects.jsonl", {"timestamp": utc_now_iso(), **payload})


def should_push_error(error: Exception) -> bool:
    if DeepSeekClient.is_transient_error(error):
        return False
    return True


def run_once(config: BotConfig) -> int:
    if config.mode != "paper":
        raise RuntimeError("Only paper mode is implemented. Refusing to run live trading.")

    ensure_dir(config.logs_dir)
    okx = OKXPublicClient()
    ai_client = DeepSeekClient(config.deepseek)
    broker = PaperBroker(config)
    notifier = BarkNotifier(config)

    mark_prices: dict[str, float] = {}
    for symbol in config.symbols:
        try:
            mark_prices[symbol] = okx.mark_price(symbol)
        except Exception as exc:
            reject(config, {"symbol": symbol, "decision": "REJECT", "reject_reason": "mark_price_error", "error": str(exc)})
            if should_push_error(exc):
                notifier.send_error(f"mark_price_error:{symbol}", str(exc))

    close_events = broker.maybe_close(mark_prices)
    for close_event in close_events:
        print(f"Closed paper position: {close_event['symbol']} {close_event['exit_reason']} net={close_event['net_pnl_usdt']:.4f}")

    can_open, reason = broker.can_open()
    if not can_open:
        reject(config, {"decision": "REJECT", "reject_reason": reason, "account": build_account_summary(broker, config)})
        print(f"No new trade: {reason}")
        return 0

    candidates: list[dict[str, Any]] = []
    for symbol in config.symbols:
        try:
            can_open_symbol, symbol_reject_reason = broker.can_open(symbol)
            if not can_open_symbol:
                reject(
                    config,
                    {
                        "symbol": symbol,
                        "candidate_action": "NONE",
                        "decision": "REJECT",
                        "reject_reason": symbol_reject_reason,
                        "account": build_account_summary(broker, config),
                    },
                )
                continue

            base = okx.candles(symbol, config.base_timeframe, 120)
            trend = okx.candles(symbol, config.trend_timeframe, 120)
            market = summarize_market(base, trend, config.high_volatility_atr_pct)
            market["mark_price"] = mark_prices.get(symbol, market["price"])
            funding = okx.funding_rate(symbol)
            market["funding_rate"] = funding["fundingRate"]
            market["next_funding_time"] = funding["nextFundingTime"]
            seconds_to_funding = okx.seconds_until(funding["nextFundingTime"])
            market["seconds_to_funding"] = seconds_to_funding

            if near_funding_window(config, seconds_to_funding):
                reject(
                    config,
                    {
                        "symbol": symbol,
                        "decision": "REJECT",
                        "reject_reason": "near_funding_time",
                        "seconds_to_funding": seconds_to_funding,
                    },
                )
                continue

            rule_candidate, prefilter_reject_reason = rule_prefilter(config, symbol, market)
            if rule_candidate is None:
                reject(
                    config,
                    {
                        "symbol": symbol,
                        "candidate_action": "NONE",
                        "decision": "REJECT",
                        "reject_stage": "rule_prefilter",
                        "reject_reason": prefilter_reject_reason,
                        "market": market,
                    },
                )
                continue

            summary = {
                "timestamp": utc_now_iso(),
                "symbol": symbol,
                "account": build_account_summary(broker, config),
                "market": market,
                "rule_candidate": rule_candidate,
                "risk_rules": {
                    "mode": config.mode,
                    "leverage": config.leverage,
                    "max_margin_per_trade_usdt": config.max_margin_per_trade_usdt,
                    "max_notional_per_trade_usdt": config.max_notional_per_trade_usdt,
                    "daily_net_loss_limit_usdt": broker.daily_loss_limit_usdt(),
                    "daily_net_loss_limit_pct": config.daily_net_loss_limit_pct,
                    "daily_profit_stop_enabled": False,
                    "maker_fee_rate": config.maker_fee_rate,
                    "taker_fee_rate": config.taker_fee_rate,
                    "entry_fee_type": config.entry_fee_type,
                    "exit_fee_type": config.exit_fee_type,
                    "allow_long": config.allow_long,
                    "allow_short": config.allow_short,
                },
            }
            ai = ai_client.decide(summary)
            append_jsonl(config.logs_dir / "snapshots.jsonl", {"summary": summary, "ai": ai})
            signal, reject_reason = build_signal(config, symbol, market, ai, rule_candidate)
            if signal is None:
                reject(
                    config,
                    {
                        "symbol": symbol,
                        "candidate_action": ai.get("preferred_action", "UNKNOWN"),
                        "decision": "REJECT",
                        "reject_reason": reject_reason,
                        "ai": ai,
                        "market": market,
                    },
                )
                continue
            candidates.append(signal)
        except Exception as exc:
            reject(config, {"symbol": symbol, "decision": "REJECT", "reject_reason": "scan_error", "error": str(exc)})
            if should_push_error(exc):
                notifier.send_error(f"scan_error:{symbol}", str(exc))

    if not candidates:
        print("No trade opened: no candidate passed AI + risk filters.")
        return 0

    opened = 0
    candidates.sort(key=lambda item: item["ai_confidence"], reverse=True)
    for signal in candidates:
        can_open_signal, open_reject_reason = broker.can_open(str(signal["symbol"]))
        if not can_open_signal:
            reject(
                config,
                {
                    "symbol": signal["symbol"],
                    "candidate_action": signal["side"],
                    "decision": "REJECT",
                    "reject_reason": open_reject_reason,
                    "account": build_account_summary(broker, config),
                },
            )
            continue
        append_jsonl(config.logs_dir / "signals.jsonl", signal)
        broker.open(signal, float(signal["entry_price"]))
        opened += 1
        print(
            "Opened paper position: "
            f"{signal['symbol']} {signal['side']} notional={signal['notional_usdt']:.2f} "
            f"tp={signal['take_profit_price']:.4f} sl={signal['stop_loss_price']:.4f} "
            f"ai={signal['ai_confidence']:.2f} source={signal['ai_source']}"
        )
    if opened == 0:
        print("No trade opened: all candidates were blocked by open-position limits.")
    return 0


def print_status(config: BotConfig) -> int:
    broker = PaperBroker(config)
    print("Paper account")
    print(f"  equity_usdt: {broker.state['equity_usdt']:.4f}")
    print(f"  daily_net_pnl_usdt: {broker.daily['daily_net_pnl_usdt']:.4f}")
    print(f"  stop_trading_today: {broker.daily['stop_trading_today']}")
    print(f"  stop_reason: {broker.daily['stop_reason']}")
    print(f"  open_positions_count: {len(broker.open_positions)}")
    print(f"  open_positions: {broker.open_positions}")
    return 0


def print_daily_summary(config: BotConfig) -> int:
    print(build_daily_summary(config))
    return 0


def notify_test(config: BotConfig) -> int:
    notifier = BarkNotifier(config)
    sent = notifier.send("AI Robot Bark test", "Bark notifications are configured for the paper trader.")
    print("Bark test sent." if sent else "Bark test not sent. Check BARK_DEVICE_KEY/BARK_KEY.")
    return 0 if sent else 1


def loop(config: BotConfig) -> int:
    notifier = BarkNotifier(config)
    while True:
        started = datetime.now(timezone.utc).isoformat()
        print(f"[{started}] scan")
        try:
            run_once(config)
            maybe_send_daily_summary(config, notifier)
        except Exception as exc:
            append_jsonl(config.logs_dir / "errors.jsonl", {"timestamp": utc_now_iso(), "error": str(exc)})
            if should_push_error(exc):
                notifier.send_error("loop", str(exc))
            print(f"Scan error: {exc}", file=sys.stderr)
        time.sleep(config.scan_interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OKX BTC/ETH paper swap AI robot")
    parser.add_argument("command", choices=["run-once", "loop", "status", "daily-summary", "notify-test"])
    parser.add_argument("--config", default="config.paper.json")
    args = parser.parse_args(argv)
    config = load_config(args.config)

    if args.command == "run-once":
        return run_once(config)
    if args.command == "loop":
        return loop(config)
    if args.command == "status":
        return print_status(config)
    if args.command == "daily-summary":
        return print_daily_summary(config)
    if args.command == "notify-test":
        return notify_test(config)
    raise ValueError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
