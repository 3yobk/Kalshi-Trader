import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from data_ingestors.kalshi_client import MarketOrderBook
from engine.execution_engine import PaperFill, PaperOrder
from engine.risk_manager import RiskDecision
from storage.sqlite_store import SQLiteStore


def test_reset_paper_history_clears_runtime_tables() -> None:
    async def run() -> None:
        db_path = Path(".test_data") / f"reset-{uuid4().hex}.sqlite3"
        store = SQLiteStore(str(db_path))
        try:
            await store.initialize()
            order = PaperOrder("TEST", "yes", 10, 1, "unit_test")
            fill = PaperFill("TEST", "yes", 1, 1, 10, 10, datetime.now(timezone.utc))
            await store.log_trade(order, fill, RiskDecision(allowed=True))
            await store.log_orderbook(MarketOrderBook("TEST", [], [], datetime.now(timezone.utc)))

            before = await store.get_report_summary()
            assert before["decisions"] == 1

            deleted = await store.reset_paper_history()
            after = await store.get_report_summary()

            assert deleted["paper_trades"] == 1
            assert deleted["market_snapshots"] == 1
            assert after["decisions"] == 0
            assert after["notional"] == 0
        finally:
            db_path.unlink(missing_ok=True)

    asyncio.run(run())
