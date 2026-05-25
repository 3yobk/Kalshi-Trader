from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


@dataclass(frozen=True)
class OrderBookLevel:
    price_cents: int
    quantity: float


@dataclass(frozen=True)
class MarketOrderBook:
    ticker: str
    yes: list[OrderBookLevel]
    no: list[OrderBookLevel]
    captured_at: datetime

    @property
    def best_yes_ask_cents(self) -> int | None:
        if not self.no:
            return None
        return 100 - max(level.price_cents for level in self.no)

    @property
    def best_yes_bid_cents(self) -> int | None:
        if not self.yes:
            return None
        return max(level.price_cents for level in self.yes)

    @property
    def spread_cents(self) -> int | None:
        bid = self.best_yes_bid_cents
        ask = self.best_yes_ask_cents
        if bid is None or ask is None:
            return None
        return max(0, ask - bid)


class KalshiClient:
    def __init__(self, base_url: str, timeout_seconds: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={"User-Agent": "safe-weather-paper-bot/0.1"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(wait=wait_exponential(multiplier=0.5, min=0.5, max=8), stop=stop_after_attempt(3))
    async def get_markets(self, event_ticker: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "status": "open"}
        if event_ticker:
            params["event_ticker"] = event_ticker
        response = await self._client.get("/markets", params=params)
        response.raise_for_status()
        payload = response.json()
        return payload.get("markets", [])

    @retry(wait=wait_exponential(multiplier=0.5, min=0.5, max=8), stop=stop_after_attempt(3))
    async def get_orderbook(self, ticker: str) -> MarketOrderBook:
        response = await self._client.get(f"/markets/{ticker}/orderbook")
        response.raise_for_status()
        response_payload = response.json()
        payload = response_payload.get("orderbook") or response_payload.get("orderbook_fp") or {}
        return MarketOrderBook(
            ticker=ticker,
            yes=_parse_levels(payload, "yes"),
            no=_parse_levels(payload, "no"),
            captured_at=datetime.now(timezone.utc),
        )

    @retry(wait=wait_exponential(multiplier=1.0, min=1.0, max=10), stop=stop_after_attempt(4))
    async def _get_markets_for_series(self, series_ticker: str, limit: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "status": "open", "series_ticker": series_ticker}
        response = await self._client.get("/markets", params=params)
        response.raise_for_status()
        return response.json().get("markets", [])

    async def get_weather_markets(self, series_tickers: list[str], limit_per_series: int = 200) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        for series_ticker in series_tickers:
            markets.extend(await self._get_markets_for_series(series_ticker, limit_per_series))
            await asyncio.sleep(0.25)
        return markets

    @retry(wait=wait_exponential(multiplier=0.5, min=0.5, max=8), stop=stop_after_attempt(3))
    async def get_market(self, ticker: str) -> dict[str, Any]:
        response = await self._client.get(f"/markets/{ticker}")
        response.raise_for_status()
        return response.json().get("market", {})


def _parse_levels(payload: dict[str, Any], side: str) -> list[OrderBookLevel]:
    cents_key = side
    dollars_key = f"{side}_dollars"
    if cents_key in payload:
        return [OrderBookLevel(price_cents=int(p), quantity=float(q)) for p, q in payload.get(cents_key, [])]
    return [
        OrderBookLevel(price_cents=round(float(p) * 100), quantity=float(q))
        for p, q in payload.get(dollars_key, [])
    ]
