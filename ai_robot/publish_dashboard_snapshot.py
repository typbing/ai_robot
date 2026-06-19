from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ai_robot.config import load_config
from ai_robot.dashboard_server import build_status
from ai_robot.live_broker import LiveBroker


DEFAULT_REPO_URL = "git@github.com:typbing/ai_robot.git"
DEFAULT_CHECKOUT_DIR = Path.home() / "ai_robot_gh_pages"


def number(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def pick_position(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": position.get("timestamp"),
        "symbol": position.get("symbol") or position.get("instId"),
        "side": position.get("side") or position.get("posSide"),
        "entry_price": number(position.get("entry_price") or position.get("avgPx")),
        "take_profit_price": number(position.get("take_profit_price") or position.get("tpTriggerPx")),
        "stop_loss_price": number(position.get("stop_loss_price") or position.get("slTriggerPx")),
        "notional_usdt": number(position.get("notional_usdt") or position.get("notionalUsd") or position.get("notional")),
        "size": str(position.get("size") or position.get("pos") or ""),
        "ai_confidence": number(position.get("ai_confidence")),
        "strategy_mode": position.get("strategy_mode"),
        "rule_market_regime": position.get("rule_market_regime"),
    }


def pick_trade(event: dict[str, Any]) -> dict[str, Any]:
    net_pnl = event.get("net_pnl_usdt")
    if net_pnl is None:
        net_pnl = event.get("estimated_net_pnl_usdt")
    return {
        "timestamp": event.get("timestamp"),
        "event": event.get("event"),
        "symbol": event.get("symbol"),
        "side": event.get("side"),
        "price": number(event.get("price")),
        "notional_usdt": number(event.get("notional_usdt")),
        "net_pnl_usdt": number(net_pnl),
        "exit_reason": event.get("exit_reason"),
        "strategy_mode": event.get("strategy_mode"),
        "rule_market_regime": event.get("rule_market_regime"),
    }


def pick_reject(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": event.get("timestamp"),
        "symbol": event.get("symbol"),
        "candidate_action": event.get("candidate_action"),
        "reject_reason": event.get("reject_reason"),
    }


def sanitized_status(config_path: str | Path) -> dict[str, Any]:
    config = load_config(config_path)
    status = build_status(config)
    account = status.get("account", {})
    daily = status.get("daily", {})
    if config.mode == "live":
        try:
            broker = LiveBroker(config)
            broker.refresh_daily_limits()
            broker._persist()
            account["equity_usdt"] = broker.daily.get("account_equity_usdt", account.get("equity_usdt"))
            daily.update(broker.daily)
        except Exception:
            pass
    return {
        "timestamp": status.get("timestamp"),
        "mode": status.get("mode"),
        "symbols": status.get("symbols", []),
        "service": status.get("service", {}),
        "account": {
            "equity_usdt": number(account.get("equity_usdt")),
            "realized_pnl_usdt": number(account.get("realized_pnl_usdt")),
            "open_positions": [
                pick_position(item)
                for item in account.get("open_positions", [])
                if isinstance(item, dict)
            ],
        },
        "daily": {
            "date": daily.get("date"),
            "daily_net_pnl_usdt": number(daily.get("daily_net_pnl_usdt")),
            "daily_net_loss_limit_usdt": number(daily.get("daily_net_loss_limit_usdt")),
            "daily_profit_stop_enabled": bool(daily.get("daily_profit_stop_enabled", False)),
            "trades_count": int(number(daily.get("trades_count"), 0)),
            "wins": int(number(daily.get("wins"), 0)),
            "losses": int(number(daily.get("losses"), 0)),
            "consecutive_losses": int(number(daily.get("consecutive_losses"), 0)),
            "stop_trading_today": bool(daily.get("stop_trading_today", False)),
            "stop_reason": daily.get("stop_reason"),
        },
        "trades": [pick_trade(item) for item in status.get("trades", []) if isinstance(item, dict)][-40:],
        "strategy_stats": status.get("strategy_stats", {}),
        "signals": [],
        "rejects": [pick_reject(item) for item in status.get("rejects", []) if isinstance(item, dict)][-30:],
        "errors": [],
        "notifications": [],
        "snapshot_policy": {
            "kind": "public_sanitized",
            "update_interval_minutes": 15,
            "redaction": "raw exchange payloads, order responses, error bodies, notification responses, and credentials are excluded",
        },
    }


def run(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, check=True, capture_output=True, text=True)


def ensure_checkout(repo_url: str, checkout_dir: Path, env: dict[str, str]) -> None:
    if (checkout_dir / ".git").exists():
        run(["git", "fetch", "origin", "gh-pages"], cwd=checkout_dir, env=env)
        run(["git", "checkout", "gh-pages"], cwd=checkout_dir, env=env)
        run(["git", "reset", "--hard", "origin/gh-pages"], cwd=checkout_dir, env=env)
        return
    if checkout_dir.exists():
        shutil.rmtree(checkout_dir)
    run(["git", "clone", "--depth", "1", "--branch", "gh-pages", repo_url, str(checkout_dir)], env=env)


def publish(config_path: str, docs_dir: Path, checkout_dir: Path, repo_url: str) -> bool:
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "status.json").write_text(
        json.dumps(sanitized_status(config_path), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.setdefault(
        "GIT_SSH_COMMAND",
        "ssh -o ConnectTimeout=10 -o BatchMode=yes -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519_github_ai_robot",
    )
    ensure_checkout(repo_url, checkout_dir, env)

    for item in checkout_dir.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    for source in docs_dir.iterdir():
        target = checkout_dir / source.name
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)

    run(["git", "config", "user.name", "typbing"], cwd=checkout_dir, env=env)
    run(["git", "config", "user.email", "typbing@users.noreply.github.com"], cwd=checkout_dir, env=env)
    run(["git", "add", "-A"], cwd=checkout_dir, env=env)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=checkout_dir, env=env)
    if diff.returncode == 0:
        return False
    run(["git", "commit", "-m", "Update sanitized dashboard snapshot"], cwd=checkout_dir, env=env)
    run(["git", "push", "origin", "gh-pages"], cwd=checkout_dir, env=env)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish a public sanitized dashboard snapshot to GitHub Pages")
    parser.add_argument("--config", default="config.live.json")
    parser.add_argument("--docs-dir", default="docs")
    parser.add_argument("--checkout-dir", default=str(DEFAULT_CHECKOUT_DIR))
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    args = parser.parse_args(argv)
    changed = publish(args.config, Path(args.docs_dir), Path(args.checkout_dir), args.repo_url)
    print("published" if changed else "no changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
