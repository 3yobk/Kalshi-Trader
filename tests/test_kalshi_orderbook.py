from datetime import datetime, timezone

from data_ingestors.kalshi_client import MarketOrderBook, OrderBookLevel, _parse_levels


def test_parse_orderbook_fp_dollar_levels() -> None:
    payload = {
        "no_dollars": [["0.9700", "5409.00"], ["0.5400", "9.00"]],
        "yes_dollars": [["0.5000", "10.00"]],
    }

    no_levels = _parse_levels(payload, "no")
    yes_levels = _parse_levels(payload, "yes")

    assert no_levels[0] == OrderBookLevel(price_cents=97, quantity=5409.0)
    assert yes_levels[0] == OrderBookLevel(price_cents=50, quantity=10.0)


def test_best_yes_ask_uses_no_book() -> None:
    orderbook = MarketOrderBook(
        ticker="KXHIGHNY-26MAY25-T72",
        yes=[OrderBookLevel(price_cents=50, quantity=10)],
        no=[OrderBookLevel(price_cents=46, quantity=9)],
        captured_at=datetime.now(timezone.utc),
    )

    assert orderbook.best_yes_ask_cents == 54
    assert orderbook.best_yes_bid_cents == 50
    assert orderbook.spread_cents == 4
