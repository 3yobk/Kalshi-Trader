from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


@dataclass(frozen=True)
class KalshiAuthCheck:
    ok: bool
    status_code: int
    key_count: int
    message: str = ""


@dataclass(frozen=True)
class LiveOrderRequest:
    ticker: str
    client_order_id: str
    side: str
    count: str
    price: str
    time_in_force: str
    self_trade_prevention_type: str
    post_only: bool
    cancel_order_on_pause: bool
    exchange_index: int

    def as_payload(self) -> dict:
        return {
            "ticker": self.ticker,
            "client_order_id": self.client_order_id,
            "side": self.side,
            "count": self.count,
            "price": self.price,
            "time_in_force": self.time_in_force,
            "self_trade_prevention_type": self.self_trade_prevention_type,
            "post_only": self.post_only,
            "cancel_order_on_pause": self.cancel_order_on_pause,
            "exchange_index": self.exchange_index,
        }


class KalshiAuthClient:
    def __init__(self, base_url: str, api_key_id: str, private_key_path: str, timeout_seconds: float = 15.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key_id = api_key_id
        self._private_key = _load_private_key(Path(private_key_path))
        self._api_root_path = urlparse(self._base_url).path.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_seconds,
            headers={"User-Agent": "safe-weather-paper-bot/0.1"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        stop=stop_after_attempt(3),
    )
    async def list_api_keys(self) -> dict:
        path = "/api_keys"
        return await self._get(path)

    async def get_balance(self) -> dict:
        return await self._get("/portfolio/balance")

    async def get_positions(self, limit: int = 100) -> dict:
        return await self._get(f"/portfolio/positions?limit={limit}")

    async def get_orders(self, limit: int = 100) -> dict:
        return await self._get(f"/portfolio/orders?limit={limit}")

    async def create_event_order(self, order: LiveOrderRequest) -> dict:
        return await self._post("/portfolio/events/orders", order.as_payload())

    async def cancel_event_order(self, order_id: str) -> dict:
        return await self._delete(f"/portfolio/events/orders/{order_id}?exchange_index=0")

    async def _get(self, path: str) -> dict:
        response = await self._client.get(path, headers=self._signed_headers("GET", path))
        response.raise_for_status()
        return response.json()

    async def _post(self, path: str, payload: dict) -> dict:
        headers = {"Content-Type": "application/json", **self._signed_headers("POST", path)}
        response = await self._client.post(path, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()

    async def _delete(self, path: str) -> dict:
        response = await self._client.delete(path, headers=self._signed_headers("DELETE", path))
        response.raise_for_status()
        return response.json()

    async def auth_check(self) -> KalshiAuthCheck:
        try:
            payload = await self.list_api_keys()
        except httpx.HTTPStatusError as exc:
            return KalshiAuthCheck(
                ok=False,
                status_code=exc.response.status_code,
                key_count=0,
                message=_safe_error_message(exc.response),
            )
        return KalshiAuthCheck(ok=True, status_code=200, key_count=len(payload.get("api_keys", [])))

    def _signed_headers(self, method: str, request_path: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        signed_path = f"{self._api_root_path}{request_path.split('?')[0]}"
        message = f"{timestamp_ms}{method.upper()}{signed_path}"
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": _sign_pss_text(self._private_key, message),
        }


def _load_private_key(path: Path) -> rsa.RSAPrivateKey:
    with path.open("rb") as key_file:
        key = serialization.load_pem_private_key(
            key_file.read(),
            password=None,
            backend=default_backend(),
        )
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Kalshi private key must be an RSA private key.")
    return key


def _sign_pss_text(private_key: rsa.RSAPrivateKey, text: str) -> str:
    signature = private_key.sign(
        text.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def build_post_only_yes_bid_order(ticker: str, quantity: int, limit_price_cents: int) -> LiveOrderRequest:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if not 1 <= limit_price_cents <= 99:
        raise ValueError("limit_price_cents must be between 1 and 99")
    return LiveOrderRequest(
        ticker=ticker,
        client_order_id=f"weatherbot-{uuid4().hex}",
        side="bid",
        count=f"{quantity:.2f}",
        price=f"{limit_price_cents / 100:.4f}",
        time_in_force="good_till_canceled",
        self_trade_prevention_type="taker_at_cross",
        post_only=True,
        cancel_order_on_pause=True,
        exchange_index=0,
    )


def _safe_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    for key in ("error", "message", "detail"):
        if key in payload:
            return str(payload[key])
    return str(payload)[:300]
