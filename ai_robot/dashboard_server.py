from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ai_robot.config import BotConfig, load_config
from ai_robot.logging_utils import utc_now_iso
from ai_robot.strategy_stats import build_strategy_stats


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(default)


def read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"raw": line})
    return rows


def service_state(name: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def build_status(config: BotConfig) -> dict[str, Any]:
    logs = config.logs_dir
    account = read_json(
        logs / ("live_state.json" if config.mode == "live" else "paper_state.json"),
        {"equity_usdt": config.starting_balance_usdt, "realized_pnl_usdt": 0.0, "open_positions": []},
    )
    daily = read_json(logs / ("live_daily_state.json" if config.mode == "live" else "daily_state.json"), {})
    if config.mode == "live":
        snapshot = read_json(logs / "live_account_snapshot.json", {})
        if snapshot:
            account["okx_balance"] = snapshot.get("balance", [])
            account["okx_positions"] = snapshot.get("positions", [])
            if snapshot.get("balance"):
                total_eq = snapshot["balance"][0].get("totalEq")
                if total_eq is not None:
                    account["equity_usdt"] = float(total_eq)
        account.setdefault("open_positions", account.get("okx_positions", []))
    return {
        "timestamp": utc_now_iso(),
        "mode": config.mode,
        "symbols": config.symbols,
        "service": {
            "paper": service_state("ai-robot-paper.service"),
            "live": service_state("ai-robot-live.service"),
            "dashboard": service_state("ai-robot-dashboard.service"),
        },
        "account": account,
        "daily": daily,
        "trades": read_jsonl_tail(logs / ("live_trades.jsonl" if config.mode == "live" else "trades.jsonl"), 40),
        "signals": read_jsonl_tail(logs / ("live_signals.jsonl" if config.mode == "live" else "signals.jsonl"), 20),
        "strategy_stats": build_strategy_stats(logs, config.mode == "live"),
        "rejects": read_jsonl_tail(logs / ("live_rejects.jsonl" if config.mode == "live" else "rejects.jsonl"), 30),
        "errors": read_jsonl_tail(logs / ("live_errors.jsonl" if config.mode == "live" else "errors.jsonl"), 20),
        "notifications": read_jsonl_tail(logs / "notifications.jsonl", 20),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    config: BotConfig
    static_dir: Path

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_json(404, {"error": "not_found"})
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self.send_json(200, build_status(self.config))
            return
        if path == "/health":
            self.send_json(200, {"ok": True, "timestamp": utc_now_iso()})
            return
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (self.static_dir / relative).resolve()
        static_root = self.static_dir.resolve()
        if static_root == target or static_root in target.parents:
            self.send_static(target)
            return
        self.send_json(404, {"error": "not_found"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only AI Robot dashboard API")
    parser.add_argument("--config", default="config.paper.json")
    parser.add_argument("--host", default="100.94.190.35")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)

    DashboardHandler.config = load_config(args.config)
    DashboardHandler.static_dir = Path("docs")
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"AI Robot dashboard API listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
