from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import Any

from ai_robot.config import BotConfig, load_config
from ai_robot.deepseek_client import DeepSeekClient
from ai_robot.indicators import summarize_market
from ai_robot.live_broker import LiveBroker, LIVE_ENABLE_VALUE
from ai_robot.logging_utils import append_jsonl, ensure_dir, utc_now_iso, write_json
from ai_robot.notifications import BarkNotifier
from ai_robot.okx_public import OKXPublicClient
from ai_robot.strategy import build_signal, near_funding_window, rule_prefilter


def reject(config: BotConfig, payload: dict[str, Any]) -> None:
    append_jsonl(config.logs_dir / "live_rejects.jsonl", {"timestamp": utc_now_iso(), **payload})


def should_push_error(error: Exception) -> bool:
    if DeepSeekClient.is_transient_error(error):
        return False
    return True


def build_account_summary(broker: LiveBroker, config: BotConfig) -> dict[str, Any]:
    return {
        "daily_net_pnl_usdt": broker.daily["daily_net_pnl_usdt"],
        "daily_loss_limit_usdt": broker.daily_loss_limit_usdt(),
        "daily_profit_stop_enabled": False,
        "open_positions": len(broker.open_positions),
        "open_symbols": sorted(broker.open_symbols()),
        "max_open_positions": config.max_open_positions,
        "stop_trading_today": broker.daily["stop_trading_today"],
        "live_enabled": broker.live_enabled,
    }


def run_once(config: BotConfig, check_exits: bool = True, scan_entries: bool = True) -> int:
    if config.mode != "live":
        raise RuntimeError("Live runner requires mode=live config.")

    ensure_dir(config.logs_dir)
    okx = OKXPublicClient()
    ai_client = DeepSeekClient(config.deepseek)
    broker = LiveBroker(config)
    notifier = BarkNotifier(config)

    mark_prices: dict[str, float] = {}
    if check_exits or scan_entries:
        for symbol in config.symbols:
            try:
                mark_prices[symbol] = okx.mark_price(symbol)
            except Exception as exc:
                reject(config, {"symbol": symbol, "decision": "REJECT", "reject_reason": "mark_price_error", "error": str(exc)})
                if should_push_error(exc):
                    notifier.send_error(f"live_mark_price_error:{symbol}", str(exc))

    if check_exits:
        close_events = broker.maybe_close(mark_prices)
        for event in close_events:
            notifier.send(
                "AI Robot live close",
                f"{event['symbol']} {event['side']} {event['exit_reason']} est_net={event['estimated_net_pnl_usdt']:+.4f} USDT",
                level="timeSensitive",
            )
            print(f"Closed live position: {event['symbol']} {event['exit_reason']} est_net={event['estimated_net_pnl_usdt']:.4f}")

    if not scan_entries:
        print("Skipped live entry scan: waiting for next entry interval.")
        return 0

    can_open, reason = broker.can_open()
    if not can_open:
        reject(config, {"decision": "REJECT", "reject_reason": reason, "account": build_account_summary(broker, config)})
        print(f"No new live trade: {reason}")
        return 0

    candidates: list[dict[str, Any]] = []
    for symbol in config.symbols:
        try:
            can_open_symbol, symbol_reason = broker.can_open(symbol)
            if not can_open_symbol:
                reject(
                    config,
                    {
                        "symbol": symbol,
                        "decision": "REJECT",
                        "reject_reason": symbol_reason,
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
                reject(config, {"symbol": symbol, "decision": "REJECT", "reject_reason": "near_funding_time"})
                continue

            rule_candidate, prefilter_reason = rule_prefilter(config, symbol, market)
            if rule_candidate is None:
                reject(
                    config,
                    {
                        "symbol": symbol,
                        "decision": "REJECT",
                        "reject_stage": "rule_prefilter",
                        "reject_reason": prefilter_reason,
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
                    "allow_long": config.allow_long,
                    "allow_short": config.allow_short,
                },
            }
            try:
                ai = ai_client.decide(summary)
            except Exception as exc:
                reject(
                    config,
                    {
                        "symbol": symbol,
                        "decision": "REJECT",
                        "candidate_action": rule_candidate.get("preferred_action", "UNKNOWN"),
                        "reject_reason": "ai_decision_error",
                        "error": str(exc),
                        "market": market,
                    },
                )
                if should_push_error(exc):
                    notifier.send_error(f"live_ai_decision_error:{symbol}", str(exc))
                continue
            append_jsonl(config.logs_dir / "live_snapshots.jsonl", {"summary": summary, "ai": ai})
            signal, signal_reason = build_signal(config, symbol, market, ai, rule_candidate)
            if signal is None:
                reject(
                    config,
                    {
                        "symbol": symbol,
                        "decision": "REJECT",
                        "candidate_action": ai.get("preferred_action", "UNKNOWN"),
                        "reject_reason": signal_reason,
                        "ai": ai,
                        "market": market,
                    },
                )
                continue
            candidates.append(signal)
        except Exception as exc:
            reject(config, {"symbol": symbol, "decision": "REJECT", "reject_reason": "scan_error", "error": str(exc)})
            if should_push_error(exc):
                notifier.send_error(f"live_scan_error:{symbol}", str(exc))

    if not candidates:
        print("No live trade opened: no candidate passed AI + risk filters.")
        return 0

    candidates.sort(key=lambda item: item["ai_confidence"], reverse=True)
    signal = candidates[0]
    try:
        position = broker.open(signal, float(signal["entry_price"]))
    except Exception as exc:
        reject(
            config,
            {
                "symbol": signal["symbol"],
                "decision": "REJECT",
                "candidate_action": signal["side"],
                "reject_reason": "live_open_error",
                "error": str(exc),
                "signal": signal,
            },
        )
        notifier.send_error(f"live_open_error:{signal['symbol']}", str(exc))
        print(f"No live trade opened: live_open_error {exc}")
        return 0
    append_jsonl(config.logs_dir / "live_signals.jsonl", signal)
    notifier.send(
        "AI Robot live open",
        f"{position['symbol']} {position['side']} notional={position['notional_usdt']:.2f} entry={position['entry_price']:.4f}",
        level="timeSensitive",
    )
    print(
        "Opened live position: "
        f"{position['symbol']} {position['side']} notional={position['notional_usdt']:.2f} "
        f"tp={position['take_profit_price']:.4f} sl={position['stop_loss_price']:.4f}"
    )
    return 0


def print_status(config: BotConfig) -> int:
    broker = LiveBroker(config)
    print("Live runner")
    print(f"  live_enabled: {broker.live_enabled}")
    print(f"  credentials_present: {broker.client.available}")
    print(f"  enable_value_required: {LIVE_ENABLE_VALUE}")
    print(f"  daily_net_pnl_usdt: {broker.daily['daily_net_pnl_usdt']:.4f}")
    print(f"  account_equity_usdt: {broker.daily.get('account_equity_usdt', 0.0):.4f}")
    print("  daily_profit_stop_enabled: False")
    print(f"  daily_loss_limit_usdt: {broker.daily_loss_limit_usdt():.4f}")
    print(f"  stop_trading_today: {broker.daily['stop_trading_today']}")
    print(f"  stop_reason: {broker.daily['stop_reason']}")
    print(f"  open_positions_count: {len(broker.open_positions)}")
    print(f"  open_positions: {broker.open_positions}")
    return 0


def check_credentials(config: BotConfig) -> int:
    broker = LiveBroker(config)
    if not broker.client.available:
        print("Missing OKX credentials.")
        return 1
    account_config = broker.client.account_config()
    balance = broker.client.balance("USDT")
    positions = broker.client.positions("SWAP")
    write_json(
        config.logs_dir / "live_account_snapshot.json",
        {
            "timestamp": utc_now_iso(),
            "account_config": account_config.get("data", []),
            "balance": balance.get("data", []),
            "positions": positions.get("data", []),
        },
    )
    print("OKX credentials are valid.")
    print(f"account_config: {account_config.get('data', [])[:1]}")
    print(f"balance: {balance.get('data', [])[:1]}")
    print(f"positions_count: {len(positions.get('data', []))}")
    return 0


def loop(config: BotConfig) -> int:
    notifier = BarkNotifier(config)
    last_exit_check = 0.0
    last_entry_scan = 0.0
    while True:
        now = time.monotonic()
        check_exits = now - last_exit_check >= config.local_exit_check_interval_seconds
        scan_entries = now - last_entry_scan >= config.scan_interval_seconds
        try:
            if check_exits or scan_entries:
                started = datetime.now(timezone.utc).isoformat()
                print(f"[{started}] live loop check_exits={check_exits} scan_entries={scan_entries}")
                run_once(config, check_exits=check_exits, scan_entries=scan_entries)
                if check_exits:
                    last_exit_check = now
                if scan_entries:
                    last_entry_scan = now
        except Exception as exc:
            append_jsonl(config.logs_dir / "live_errors.jsonl", {"timestamp": utc_now_iso(), "error": str(exc)})
            if should_push_error(exc):
                notifier.send_error("live_loop", str(exc))
            print(f"Live scan error: {exc}", file=sys.stderr)
        time.sleep(min(30, config.scan_interval_seconds, config.local_exit_check_interval_seconds))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OKX BTC/ETH small live swap runner")
    parser.add_argument("command", choices=["status", "check-credentials", "run-once", "loop"])
    parser.add_argument("--config", default="config.live.json")
    args = parser.parse_args(argv)
    config = load_config(args.config)

    if args.command == "status":
        return print_status(config)
    if args.command == "check-credentials":
        return check_credentials(config)
    if args.command == "run-once":
        return run_once(config)
    if args.command == "loop":
        return loop(config)
    raise ValueError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
