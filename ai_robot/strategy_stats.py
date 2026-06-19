from __future__ import annotations

import json
import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def trade_net_pnl(event: dict[str, Any]) -> float:
    value = event.get("net_pnl_usdt")
    if value is None:
        value = event.get("estimated_net_pnl_usdt")
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def new_bucket() -> dict[str, Any]:
    return {
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "net_pnl_usdt": 0.0,
        "gross_win_usdt": 0.0,
        "gross_loss_usdt": 0.0,
    }


def finalize(bucket: dict[str, Any]) -> dict[str, Any]:
    closed = int(bucket["closed_trades"])
    wins = int(bucket["wins"])
    losses = int(bucket["losses"])
    bucket["win_rate"] = wins / closed if closed else 0.0
    bucket["avg_win_usdt"] = float(bucket["gross_win_usdt"]) / wins if wins else 0.0
    bucket["avg_loss_usdt"] = float(bucket["gross_loss_usdt"]) / losses if losses else 0.0
    bucket["net_pnl_usdt"] = round(float(bucket["net_pnl_usdt"]), 8)
    bucket["gross_win_usdt"] = round(float(bucket["gross_win_usdt"]), 8)
    bucket["gross_loss_usdt"] = round(float(bucket["gross_loss_usdt"]), 8)
    return bucket


def build_strategy_stats(logs_dir: Path, live: bool) -> dict[str, Any]:
    trades = read_jsonl(logs_dir / ("live_trades.jsonl" if live else "trades.jsonl"))
    signals = read_jsonl(logs_dir / ("live_signals.jsonl" if live else "signals.jsonl"))
    signal_by_id = {str(item.get("trade_id")): item for item in signals if item.get("trade_id")}
    open_by_id: dict[str, dict[str, Any]] = {}
    by_strategy: dict[str, dict[str, Any]] = defaultdict(new_bucket)
    by_regime: dict[str, dict[str, Any]] = defaultdict(new_bucket)
    by_symbol: dict[str, dict[str, Any]] = defaultdict(new_bucket)

    for event in trades:
        trade_id = str(event.get("trade_id") or "")
        if event.get("event") == "OPEN" and trade_id:
            open_by_id[trade_id] = event
            continue
        if event.get("event") != "CLOSE":
            continue
        signal = signal_by_id.get(trade_id, {})
        opened = open_by_id.get(trade_id, {})
        strategy = str(
            event.get("strategy_mode")
            or opened.get("strategy_mode")
            or signal.get("strategy_mode")
            or "unknown"
        )
        regime = str(
            event.get("rule_market_regime")
            or opened.get("rule_market_regime")
            or signal.get("rule_market_regime")
            or "unknown"
        )
        symbol = str(event.get("symbol") or opened.get("symbol") or signal.get("symbol") or "unknown")
        pnl = trade_net_pnl(event)
        for bucket in (by_strategy[strategy], by_regime[regime], by_symbol[symbol]):
            bucket["closed_trades"] += 1
            bucket["net_pnl_usdt"] += pnl
            if pnl > 0:
                bucket["wins"] += 1
                bucket["gross_win_usdt"] += pnl
            else:
                bucket["losses"] += 1
                bucket["gross_loss_usdt"] += pnl

    return {
        "by_strategy": {key: finalize(value) for key, value in sorted(by_strategy.items())},
        "by_regime": {key: finalize(value) for key, value in sorted(by_regime.items())},
        "by_symbol": {key: finalize(value) for key, value in sorted(by_symbol.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize realized PnL by strategy, regime, and symbol")
    parser.add_argument("logs_dir", type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build_strategy_stats(args.logs_dir, args.live), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
