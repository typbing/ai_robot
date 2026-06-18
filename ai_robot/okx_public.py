from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


OKX_BASE_URL = "https://www.okx.com"


@dataclass(frozen=True)
class Candle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp_ms / 1000, tz=timezone.utc)


class OKXPublicClient:
    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode(params)
        url = f"{OKX_BASE_URL}{path}?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": "ai-robot-paper/0.1"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX error {payload.get('code')}: {payload.get('msg')}")
        return payload

    def candles(self, inst_id: str, bar: str, limit: int = 120) -> list[Candle]:
        payload = self._get("/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": str(limit)})
        candles: list[Candle] = []
        # OKX returns newest first. Reverse to chronological order.
        for row in reversed(payload["data"]):
            candles.append(
                Candle(
                    timestamp_ms=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        return candles

    def mark_price(self, inst_id: str) -> float:
        payload = self._get("/api/v5/public/mark-price", {"instType": "SWAP", "instId": inst_id})
        data = payload["data"]
        if not data:
            raise RuntimeError(f"No mark price for {inst_id}")
        return float(data[0]["markPx"])

    def funding_rate(self, inst_id: str) -> dict[str, Any]:
        payload = self._get("/api/v5/public/funding-rate", {"instId": inst_id})
        data = payload["data"]
        if not data:
            return {"fundingRate": 0.0, "nextFundingTime": None}
        row = data[0]
        return {
            "fundingRate": float(row.get("fundingRate") or 0.0),
            "nextFundingTime": int(row["nextFundingTime"]) if row.get("nextFundingTime") else None,
        }

    @staticmethod
    def seconds_until(timestamp_ms: int | None) -> float | None:
        if timestamp_ms is None:
            return None
        return timestamp_ms / 1000 - time.time()

    def instrument(self, inst_id: str) -> dict[str, Any]:
        payload = self._get("/api/v5/public/instruments", {"instType": "SWAP", "instId": inst_id})
        data = payload["data"]
        if not data:
            raise RuntimeError(f"No instrument metadata for {inst_id}")
        return data[0]
