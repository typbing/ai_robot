from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ai_robot.config import BotConfig
from ai_robot.logging_utils import append_jsonl, read_json, utc_now_iso, write_json
from ai_robot.paper_broker import PaperBroker


DENVER_TZ = ZoneInfo("America/Denver")


@dataclass(frozen=True)
class BarkSettings:
    enabled: bool
    device_key: str
    base_url: str
    group: str
    sound: str
    level: str
    icon: str
    timeout_seconds: int


def load_bark_settings() -> BarkSettings:
    device_key = os.environ.get("BARK_DEVICE_KEY", "").strip() or os.environ.get("BARK_KEY", "").strip()
    return BarkSettings(
        enabled=os.environ.get("BARK_ENABLED", "1").strip().lower() not in {"0", "false", "no"},
        device_key=device_key,
        base_url=os.environ.get("BARK_BASE_URL", "https://api.day.app").strip().rstrip("/"),
        group=os.environ.get("BARK_GROUP", "AI Robot").strip(),
        sound=os.environ.get("BARK_SOUND", "").strip(),
        level=os.environ.get("BARK_LEVEL", "active").strip(),
        icon=os.environ.get("BARK_ICON", "").strip(),
        timeout_seconds=int(os.environ.get("BARK_TIMEOUT_SECONDS", "15")),
    )


class BarkNotifier:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.settings = load_bark_settings()
        self.log_path = config.logs_dir / "notifications.jsonl"

    @property
    def available(self) -> bool:
        return self.settings.enabled and bool(self.settings.device_key)

    def send(self, title: str, body: str, *, level: str | None = None, sound: str | None = None) -> bool:
        if not self.available:
            append_jsonl(
                self.log_path,
                {
                    "timestamp": utc_now_iso(),
                    "channel": "bark",
                    "status": "skipped",
                    "reason": "missing_bark_device_key",
                    "title": title,
                },
            )
            return False

        params: dict[str, str] = {"group": self.settings.group}
        push_level = level if level is not None else self.settings.level
        push_sound = sound if sound is not None else self.settings.sound
        if push_level:
            params["level"] = push_level
        if push_sound:
            params["sound"] = push_sound
        if self.settings.icon:
            params["icon"] = self.settings.icon

        path = "/".join(
            [
                urllib.parse.quote(self.settings.device_key, safe=""),
                urllib.parse.quote(title, safe=""),
                urllib.parse.quote(body, safe=""),
            ]
        )
        query = urllib.parse.urlencode(params)
        url = f"{self.settings.base_url}/{path}?{query}"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "ai-robot-paper/0.1"})
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                payload = response.read().decode("utf-8", errors="replace")
                status_code = response.status
            ok = 200 <= status_code < 300
            append_jsonl(
                self.log_path,
                {
                    "timestamp": utc_now_iso(),
                    "channel": "bark",
                    "status": "sent" if ok else "failed",
                    "http_status": status_code,
                    "response": payload[:500],
                    "title": title,
                },
            )
            return ok
        except Exception as exc:
            append_jsonl(
                self.log_path,
                {
                    "timestamp": utc_now_iso(),
                    "channel": "bark",
                    "status": "failed",
                    "error": str(exc),
                    "title": title,
                },
            )
            return False

    def send_error(self, source: str, error: str) -> bool:
        body = f"Source: {source}\nTime: {datetime.now(DENVER_TZ).isoformat(timespec='seconds')}\n{error[:1200]}"
        return self.send("AI Robot error", body, level="timeSensitive")


def local_date_from_iso(value: str) -> str | None:
    try:
        return datetime.fromisoformat(value).astimezone(DENVER_TZ).date().isoformat()
    except ValueError:
        return None


def read_todays_trades(config: BotConfig, local_date: str) -> list[dict[str, Any]]:
    trades_path = config.logs_dir / "trades.jsonl"
    if not trades_path.exists():
        return []
    trades: list[dict[str, Any]] = []
    with trades_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp = str(event.get("timestamp") or "")
            if local_date_from_iso(timestamp) == local_date:
                trades.append(event)
    return trades


def format_trade(event: dict[str, Any]) -> str:
    symbol = event.get("symbol", "?")
    action = event.get("event", "?")
    side = event.get("side", "?")
    price = float(event.get("price", 0.0))
    if action == "CLOSE":
        pnl = float(event.get("net_pnl_usdt", 0.0))
        reason = event.get("exit_reason", "?")
        return f"{action} {symbol} {side} @ {price:.4f}, net {pnl:+.4f}, {reason}"
    return f"{action} {symbol} {side} @ {price:.4f}, notional {float(event.get('notional_usdt', 0.0)):.2f}"


def build_daily_summary(config: BotConfig) -> str:
    broker = PaperBroker(config)
    now = datetime.now(DENVER_TZ)
    local_date = now.date().isoformat()
    trades = read_todays_trades(config, local_date)
    trade_lines = [format_trade(event) for event in trades] or ["No trades today."]
    positions = broker.open_positions
    position_lines = [
        (
            f"{position.get('symbol')} {position.get('side')} entry {float(position.get('entry_price', 0.0)):.4f} "
            f"TP {float(position.get('take_profit_price', 0.0)):.4f} SL {float(position.get('stop_loss_price', 0.0)):.4f}"
        )
        for position in positions
    ] or ["No open positions."]

    return "\n".join(
        [
            f"Date: {local_date} {now.strftime('%H:%M %Z')}",
            f"Equity: {float(broker.state['equity_usdt']):.4f} USDT",
            f"Daily net PnL: {float(broker.daily['daily_net_pnl_usdt']):+.4f} USDT",
            f"Realized PnL: {float(broker.state.get('realized_pnl_usdt', 0.0)):+.4f} USDT",
            f"Open positions: {len(positions)}",
            "Trades:",
            *trade_lines,
            "Open:",
            *position_lines,
        ]
    )


def notification_state_path(config: BotConfig) -> Path:
    return config.logs_dir / "notification_state.json"


def maybe_send_daily_summary(config: BotConfig, notifier: BarkNotifier) -> bool:
    now = datetime.now(DENVER_TZ)
    if now.hour < 17:
        return False
    state_path = notification_state_path(config)
    state = read_json(state_path, {"last_daily_summary_date": None})
    today = now.date().isoformat()
    if state.get("last_daily_summary_date") == today:
        return False
    sent = notifier.send("AI Robot daily summary", build_daily_summary(config))
    if sent:
        state["last_daily_summary_date"] = today
        state["last_daily_summary_sent_at"] = utc_now_iso()
        write_json(state_path, state)
    return sent
