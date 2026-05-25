import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from engine.execution_engine import PaperFill, PaperOrder
from engine.risk_manager import RiskDecision
from main import _make_external_weather_clients, _missing_external_weather_keys, _open_orders, _nonzero_positions, _order_age_minutes
from config import RuntimeSettings
from storage.sqlite_store import SQLiteStore


def test_latest_allowed_paper_trade() -> None:
    async def run() -> None:
        db_path = Path(".test_data") / f"status-{uuid4().hex}.sqlite3"
        store = SQLiteStore(str(db_path))
        try:
            await store.initialize()
            order = PaperOrder("TEST", "yes", 25, 2, "edge_trade")
            fill = PaperFill("TEST", "yes", 2, 2, 25, 25, datetime.now(timezone.utc))
            await store.log_trade(order, fill, RiskDecision(allowed=True))

            latest = await store.get_latest_allowed_paper_trade()

            assert latest is not None
            assert latest["ticker"] == "TEST"
            assert latest["filled_quantity"] == 2
        finally:
            db_path.unlink(missing_ok=True)

    asyncio.run(run())


def test_open_orders_filters_canceled_history() -> None:
    payload = {"orders": [{"status": "canceled"}, {"status": "resting"}, {"status": "open"}]}

    assert len(_open_orders(payload)) == 2


def test_nonzero_positions_filters_flat_positions() -> None:
    payload = {"market_positions": [{"position": 0}, {"position": "1.00"}, {"position": "-2"}]}

    assert len(_nonzero_positions(payload)) == 2


def test_order_age_minutes_reads_epoch_milliseconds() -> None:
    created = datetime.now(timezone.utc) - timedelta(minutes=5)
    order = {"created_ts_ms": int(created.timestamp() * 1000)}

    age = _order_age_minutes(order)

    assert age is not None
    assert 4 <= age <= 6


def test_external_weather_clients_include_climatology_without_keys() -> None:
    settings = RuntimeSettings()

    clients = _make_external_weather_clients(settings)

    assert [client.__class__.__name__ for client in clients] == ["ClimatologyClient"]
    assert "TOMORROW_API_KEY" in _missing_external_weather_keys(settings)
