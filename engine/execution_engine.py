from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from data_ingestors.kalshi_client import MarketOrderBook, OrderBookLevel


@dataclass(frozen=True)
class PaperOrder:
    ticker: str
    side: str
    limit_price_cents: int
    quantity: int
    reason: str


@dataclass(frozen=True)
class PaperFill:
    ticker: str
    side: str
    requested_quantity: int
    filled_quantity: int
    average_price_cents: float | None
    limit_price_cents: int
    created_at: datetime


class PaperExecutionEngine:
    def submit_limit_order(self, order: PaperOrder, orderbook: MarketOrderBook) -> PaperFill:
        if order.side != "yes":
            raise ValueError("Starter implementation only simulates YES buys.")
        levels = _yes_ask_levels(orderbook)
        remaining = order.quantity
        cost = 0
        filled = 0
        for level in levels:
            if level.price_cents > order.limit_price_cents or remaining <= 0:
                break
            take = min(remaining, int(level.quantity))
            filled += take
            remaining -= take
            cost += take * level.price_cents

        average = cost / filled if filled else None
        return PaperFill(
            ticker=order.ticker,
            side=order.side,
            requested_quantity=order.quantity,
            filled_quantity=filled,
            average_price_cents=average,
            limit_price_cents=order.limit_price_cents,
            created_at=datetime.now(timezone.utc),
        )


def _yes_ask_levels(orderbook: MarketOrderBook) -> list[OrderBookLevel]:
    # Selling NO at p cents is economically equivalent to buying YES at 100 - p cents.
    return sorted(
        [OrderBookLevel(price_cents=100 - level.price_cents, quantity=level.quantity) for level in orderbook.no],
        key=lambda level: level.price_cents,
    )
