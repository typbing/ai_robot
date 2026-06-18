# AI Robot - OKX BTC/ETH Trader

This is a guarded BTC/ETH OKX USDT perpetual swap trading robot.

Current guarded live defaults:

- Mode: small live OKX trading, with paper mode retained for local testing
- Instruments: `BTC-USDT-SWAP`, `ETH-USDT-SWAP`
- Margin: isolated
- Leverage: 5x
- Starting equity: 200 USDT
- Daily profit stop: disabled
- Daily loss stop: approximately `-1%` of current account equity
- Fees: maker 0.0200%, taker 0.0500%
- Max open positions: 2
- One open position per symbol
- Max margin per trade: 40 USDT
- Max notional per trade: 200 USDT
- Target net profit per trade: 1.5 USDT
- Entry/exit fee assumptions: taker/taker
- Native OKX attached TP/SL is requested on entry; the local polling exit remains as a backup.

## Setup

Use Python 3.11+:

```powershell
py --version
```

Optional DeepSeek key:

```powershell
$env:DEEPSEEK_API_KEY="your_key_here"
```

If no key is set, the robot still runs in rule-only fallback mode.

Optional Bark push notifications:

```powershell
$env:BARK_DEVICE_KEY="your_bark_device_key"
$env:BARK_BASE_URL="https://api.day.app"
$env:BARK_GROUP="AI Robot"
```

Bark is used for live open/close notifications, serious runtime errors, and daily summaries.

## DeepSeek Call Policy

The bot does not call DeepSeek on every 5-minute scan.

Each scan follows this order:

1. Update any open position and check take-profit / stop-loss.
2. Skip symbols that already have an open position.
3. Stop opening new trades if the daily loss limit or consecutive-loss limit is reached.
4. Pull OKX BTC/ETH market data.
5. Run deterministic rule prefilter.
6. Call DeepSeek only when the rule prefilter finds a LONG or SHORT candidate.

That means normal no-op scans cost zero DeepSeek calls. BTC and ETH can both be held at the same time, up to `max_open_positions`.

## Run One Scan

```powershell
py -m ai_robot.runner run-once --config config.paper.json
```

## Small Live Runner

The live runner is locked by default. It requires OKX credentials and an explicit real-money gate:

```bash
OKX_API_KEY=...
OKX_API_SECRET=...
OKX_API_PASSPHRASE=...
LIVE_TRADING_ENABLED=I_UNDERSTAND_REAL_MONEY_IS_AT_RISK
```

Check credentials without placing orders:

```powershell
py -m ai_robot.live_runner check-credentials --config config.live.json
```

Run one guarded live scan:

```powershell
py -m ai_robot.live_runner run-once --config config.live.json
```

Live defaults are intentionally small. The bot does not stop opening trades after a daily profit target; it keeps only the daily loss stop, consecutive-loss stop, max-position cap, and one-position-per-symbol cap.

## Run Loop

```powershell
py -m ai_robot.runner loop --config config.paper.json
```

The loop interval is controlled by `scan_interval_seconds` in `config.paper.json`.

## Check Status

```powershell
py -m ai_robot.runner status --config config.paper.json
```

## Bark Test And Daily Summary

```powershell
py -m ai_robot.runner notify-test --config config.paper.json
py -m ai_robot.runner daily-summary --config config.paper.json
```

The loop sends one daily Bark summary after 5:00 PM America/Denver time.

## Read-Only Dashboard

The dashboard API is read-only and serves account/log state from `logs_live/` in live mode.

```powershell
py -m ai_robot.dashboard_server --config config.live.json --host 100.94.190.35 --port 8787
```

The static frontend lives in `docs/` and defaults to `http://100.94.190.35:8787`.

For GitHub Pages, publish the contents of `docs/` from the `gh-pages` branch.
The server can publish a public sanitized snapshot every 15 minutes with
`deploy/ai-robot-dashboard-snapshot.timer`.

## Logs

Runtime files are stored under `logs/` or `logs_live/` by default and should not be committed:

- `signals.jsonl`: accepted candidate signals
- `rejects.jsonl`: rejected candidates and reasons
- `trades.jsonl`: paper fills
- `daily_state.json`: current day state
- `paper_state.json`: balance and open position state
- `snapshots.jsonl`: market and AI snapshots
- `notifications.jsonl`: Bark notification attempts and errors

## Safety

Live trading uses real money. Keep API withdrawals disabled and use an IP whitelist where possible.
