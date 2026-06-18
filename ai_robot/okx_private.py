from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
from contextlib import contextmanager
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


OKX_BASE_URL = "https://www.okx.com"


@dataclass(frozen=True)
class OKXCredentials:
    api_key: str
    api_secret: str
    passphrase: str


def load_okx_credentials() -> OKXCredentials:
    return OKXCredentials(
        api_key=os.environ.get("OKX_API_KEY", "").strip(),
        api_secret=os.environ.get("OKX_API_SECRET", "").strip(),
        passphrase=os.environ.get("OKX_API_PASSPHRASE", "").strip(),
    )


def utc_timestamp_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@contextmanager
def prefer_ipv4() -> Any:
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_ipv4(host: str, port: int, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0) -> Any:
        return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = getaddrinfo_ipv4
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


class OKXPrivateClient:
    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds
        self.credentials = load_okx_credentials()

    @property
    def available(self) -> bool:
        return bool(self.credentials.api_key and self.credentials.api_secret and self.credentials.passphrase)

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(self.credentials.api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("Missing OKX_API_KEY, OKX_API_SECRET, or OKX_API_PASSPHRASE")
        request_path = path
        if params:
            query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
            request_path = f"{path}?{query}"
        body_text = "" if body is None else json.dumps(body, separators=(",", ":"))
        timestamp = utc_timestamp_ms()
        headers = {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": self.credentials.api_key,
            "OK-ACCESS-SIGN": self._sign(timestamp, method, request_path, body_text),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.credentials.passphrase,
            "User-Agent": "ai-robot-live/0.1",
        }
        request = urllib.request.Request(
            f"{OKX_BASE_URL}{request_path}",
            data=body_text.encode("utf-8") if body is not None else None,
            headers=headers,
            method=method.upper(),
        )
        try:
            with prefer_ipv4(), urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OKX HTTP {exc.code}: {body}") from exc
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX error {payload.get('code')}: {payload.get('msg')} {payload.get('data')}")
        return payload

    def account_config(self) -> dict[str, Any]:
        return self._request("GET", "/api/v5/account/config")

    def balance(self, ccy: str = "USDT") -> dict[str, Any]:
        return self._request("GET", "/api/v5/account/balance", {"ccy": ccy})

    def positions(self, inst_type: str = "SWAP") -> dict[str, Any]:
        return self._request("GET", "/api/v5/account/positions", {"instType": inst_type})

    def set_leverage(self, inst_id: str, lever: int, mgn_mode: str, pos_side: str | None = None) -> dict[str, Any]:
        body = {"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode}
        if pos_side:
            body["posSide"] = pos_side
        return self._request("POST", "/api/v5/account/set-leverage", body=body)

    def place_market_order(
        self,
        *,
        inst_id: str,
        td_mode: str,
        side: str,
        size: str,
        client_order_id: str,
        reduce_only: bool = False,
        pos_side: str | None = None,
        attach_algo_ords: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        order: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": "market",
            "sz": size,
            "clOrdId": client_order_id[-32:],
        }
        if pos_side:
            order["posSide"] = pos_side
        if reduce_only:
            order["reduceOnly"] = "true"
        if attach_algo_ords:
            order["attachAlgoOrds"] = attach_algo_ords
        return self._request("POST", "/api/v5/trade/order", body=order)
