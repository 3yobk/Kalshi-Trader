from datetime import datetime, timezone

from data_ingestors.kalshi_client import MarketOrderBook, OrderBookLevel
from engine.execution_engine import PaperExecutionEngine, PaperOrder


def test_paper_fill_uses_weighted_average_yes_ask() -> None:
    orderbook = MarketOrderBook(
        ticker="TEST",
        yes=[],
        no=[
            OrderBookLevel(price_cents=97, quantity=100),  # YES ask 3c
            OrderBookLevel(price_cents=96, quantity=100),  # YES ask 4c
        ],
        captured_at=datetime.now(timezone.utc),
    )
    order = PaperOrder(ticker="TEST", side="yes", limit_price_cents=4, quantity=150, reason="unit_test")

    fill = PaperExecutionEngine().submit_limit_order(order, orderbook)

    assert fill.filled_quantity == 150
    assert fill.average_price_cents == (100 * 3 + 50 * 4) / 150
