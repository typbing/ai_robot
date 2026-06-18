from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from ai_robot.config import DeepSeekConfig


SYSTEM_PROMPT = """You are a conservative crypto perpetual swap trading risk analyst.
Return only valid JSON. Do not use markdown.
The account is small. The robot may trade paper or small live size, so your decision must be conservative and realistic.
You are a second-pass reviewer. A deterministic rule prefilter has already found one or more strategy options.
Choose exactly one supplied strategy option when its market structure, volatility, and fee-adjusted reward are acceptable.
Return HOLD if none of the supplied options is clean enough for a small live account.
If daily_loss_stop_enabled is false, do not treat daily_net_loss_limit_usdt as an active trading block.
Valid preferred_action values: LONG, SHORT, HOLD.
Valid market_regime values: bullish_trend, bearish_trend, range, high_volatility, low_quality.
Valid strategy_mode values: trend_pullback, trend_momentum, range_reversal, none.
"""


def fallback_ai_decision(summary: dict[str, Any]) -> dict[str, Any]:
    if "rule_candidate" in summary:
        candidate = summary["rule_candidate"]
        return {
            "allow_trade": True,
            "symbol": summary["symbol"],
            "market_regime": candidate["market_regime"],
            "preferred_action": candidate["preferred_action"],
            "strategy_mode": candidate.get("strategy_mode", "trend_pullback"),
            "confidence": 0.71,
            "risk_level": "medium",
            "reason": f"Rule fallback accepted prefiltered candidate. {candidate['reason']}",
            "source": "rule_fallback",
        }

    market = summary["market"]
    regime = market["market_regime_rule"]
    rsi_value = float(market["rsi_base"])
    action = "HOLD"
    allow = False
    confidence = 0.5
    reason = "Rule fallback found no high-quality setup."

    if regime == "bullish_trend" and 42 <= rsi_value <= 68:
        action = "LONG"
        allow = True
        confidence = 0.72
        reason = "Rule fallback: bullish trend with RSI in acceptable pullback range."
    elif regime == "bearish_trend" and 32 <= rsi_value <= 58:
        action = "SHORT"
        allow = True
        confidence = 0.72
        reason = "Rule fallback: bearish trend with RSI in acceptable rebound-failure range."
    elif regime == "range":
        if rsi_value <= 35:
            action = "LONG"
            allow = True
            confidence = 0.68
            reason = "Rule fallback: range market with oversold RSI."
        elif rsi_value >= 65:
            action = "SHORT"
            allow = True
            confidence = 0.68
            reason = "Rule fallback: range market with overbought RSI."

    return {
        "allow_trade": allow,
        "symbol": summary["symbol"],
        "market_regime": regime,
        "preferred_action": action,
        "strategy_mode": "none",
        "confidence": confidence,
        "risk_level": "medium" if allow else "high",
        "reason": reason,
        "source": "rule_fallback",
    }


class DeepSeekClient:
    def __init__(self, config: DeepSeekConfig) -> None:
        self.config = config
        self.api_key = self._read_api_key(config.api_key_env)

    @staticmethod
    def _read_api_key(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if value:
            return value
        if os.name != "nt":
            return ""
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                registry_value, _ = winreg.QueryValueEx(key, name)
            return str(registry_value).strip()
        except OSError:
            return ""

    @property
    def available(self) -> bool:
        return self.config.enabled and bool(self.api_key)

    @staticmethod
    def is_transient_error(error: Exception) -> bool:
        text = str(error).lower()
        return any(
            marker in text
            for marker in (
                "temporary failure in name resolution",
                "timed out",
                "timeout",
                "connection reset",
                "connection aborted",
                "remote end closed connection",
                "network is unreachable",
            )
        )

    def decide(self, summary: dict[str, Any]) -> dict[str, Any]:
        if not self.available:
            return fallback_ai_decision(summary)

        body = {
            "model": self.config.model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "Decide whether the robot may open one BTC/ETH USDT perpetual swap trade now.",
                            "required_json_schema": {
                                "allow_trade": "boolean",
                                "symbol": "string",
                                "market_regime": "string",
                                "preferred_action": "LONG|SHORT|HOLD",
                                "strategy_mode": "trend_pullback|trend_momentum|range_reversal|none",
                                "confidence": "number from 0 to 1",
                                "risk_level": "low|medium|high",
                                "reason": "short string",
                            },
                            "summary": summary,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            self.config.base_url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "ai-robot-paper/0.1",
            },
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"DeepSeek HTTP {exc.code}: {body_text[:500]}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt == 2:
                    raise RuntimeError(f"DeepSeek transient network error after retries: {exc}") from exc
                time.sleep(1.5 * (attempt + 1))
        else:
            raise RuntimeError(f"DeepSeek transient network error after retries: {last_error}")
        content = payload["choices"][0]["message"]["content"]
        decision = json.loads(content)
        decision["source"] = "deepseek"
        return decision
