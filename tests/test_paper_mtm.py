import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from engine.model import ProbabilityEstimate
from engine.execution_engine import PaperFill, PaperOrder
from engine.risk_manager import RiskDecision
from storage.sqlite_store import SQLiteStore


def test_open_paper_positions_are_grouped_by_ticker() -> None:
    async def run() -> None:
        db_path = Path(".test_data") / f"mtm-{uuid4().hex}.sqlite3"
        store = SQLiteStore(str(db_path))
        try:
            await store.initialize()
            for price in (40, 60):
                order = PaperOrder("TEST", "yes", price, 1, "edge_trade")
                fill = PaperFill("TEST", "yes", 1, 1, price, price, datetime.now(timezone.utc))
                await store.log_trade(order, fill, RiskDecision(allowed=True))

            positions = await store.get_open_paper_positions()

            assert len(positions) == 1
            assert positions[0]["ticker"] == "TEST"
            assert positions[0]["quantity"] == 2
            assert positions[0]["average_entry_cents"] == 50
            assert positions[0]["cost_dollars"] == 1
        finally:
            db_path.unlink(missing_ok=True)

    asyncio.run(run())


def test_filled_paper_trades_returns_only_fills_newest_first() -> None:
    async def run() -> None:
        db_path = Path(".test_data") / f"ledger-{uuid4().hex}.sqlite3"
        store = SQLiteStore(str(db_path))
        try:
            await store.initialize()
            skipped_order = PaperOrder("SKIP", "yes", 50, 0, "risk_rejected")
            skipped_fill = PaperFill("SKIP", "yes", 0, 0, None, 50, datetime.now(timezone.utc))
            await store.log_trade(skipped_order, skipped_fill, RiskDecision(allowed=False, reasons=["test"]))

            first_order = PaperOrder("FIRST", "yes", 40, 2, "edge_trade")
            first_fill = PaperFill("FIRST", "yes", 2, 2, 40, 40, datetime.now(timezone.utc))
            await store.log_trade(first_order, first_fill, RiskDecision(allowed=True))

            second_order = PaperOrder("SECOND", "yes", 30, 3, "edge_trade")
            second_fill = PaperFill("SECOND", "yes", 3, 3, 30, 30, datetime.now(timezone.utc))
            await store.log_trade(second_order, second_fill, RiskDecision(allowed=True))

            trades = await store.get_filled_paper_trades(limit=10)

            assert [trade["ticker"] for trade in trades] == ["SECOND", "FIRST"]
            assert trades[0]["filled_quantity"] == 3
            assert trades[1]["average_price_cents"] == 40
        finally:
            db_path.unlink(missing_ok=True)

    asyncio.run(run())


def test_paper_performance_summarizes_edges_outcomes_and_skips() -> None:
    async def run() -> None:
        db_path = Path(".test_data") / f"performance-{uuid4().hex}.sqlite3"
        store = SQLiteStore(str(db_path))
        try:
            await store.initialize()
            generated_at = datetime.now(timezone.utc)
            await store.log_probability(
                ProbabilityEstimate(
                    ticker="WIN",
                    station="TEST",
                    probability=0.70,
                    fair_yes_cents=70,
                    forecast_max_f=75,
                    adjusted_mean_f=75,
                    sigma_f=4,
                    max_data_age_minutes=5,
                    generated_at=generated_at,
                )
            )
            await store.log_probability(
                ProbabilityEstimate(
                    ticker="SKIP",
                    station="TEST",
                    probability=0.20,
                    fair_yes_cents=20,
                    forecast_max_f=75,
                    adjusted_mean_f=75,
                    sigma_f=4,
                    max_data_age_minutes=5,
                    generated_at=generated_at,
                )
            )

            win_order = PaperOrder("WIN", "yes", 40, 2, "edge_trade")
            win_fill = PaperFill("WIN", "yes", 2, 2, 40, 40, generated_at)
            await store.log_trade(win_order, win_fill, RiskDecision(allowed=True))

            skip_order = PaperOrder("SKIP", "yes", 50, 0, "risk_rejected")
            skip_fill = PaperFill("SKIP", "yes", 0, 0, None, 50, generated_at)
            await store.log_trade(skip_order, skip_fill, RiskDecision(allowed=False, reasons=["edge_below_threshold"]))

            await store.record_outcome("WIN", True)
            performance = await store.get_paper_performance()

            assert performance["summary"]["decisions"] == 2
            assert performance["summary"]["filled_trades"] == 1
            assert performance["summary"]["filled_contracts"] == 2
            assert round(performance["edge"]["avg"], 6) == 0.30
            assert performance["realized"]["wins"] == 1
            assert round(performance["realized"]["realized_pnl"], 6) == 1.2
            assert performance["skip_reasons"] == [("edge_below_threshold", 1)]
        finally:
            db_path.unlink(missing_ok=True)

    asyncio.run(run())
