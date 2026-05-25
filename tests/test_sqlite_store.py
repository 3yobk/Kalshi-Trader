import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from engine.execution_engine import PaperFill, PaperOrder
from engine.model import ProbabilityEstimate
from engine.risk_manager import RiskDecision
from storage.sqlite_store import SQLiteStore


def test_daily_pnl_uses_utc_trade_date() -> None:
    async def run() -> None:
        db_path = Path(".test_data") / f"bot-{uuid4().hex}.sqlite3"
        store = SQLiteStore(str(db_path))
        try:
            await store.initialize()
            order = PaperOrder(
                ticker="KXHIGHNY-26MAY25-T72",
                side="yes",
                limit_price_cents=54,
                quantity=9,
                reason="edge_trade",
            )
            fill = PaperFill(
                ticker=order.ticker,
                side=order.side,
                requested_quantity=9,
                filled_quantity=9,
                average_price_cents=54,
                limit_price_cents=54,
                created_at=datetime.now(timezone.utc),
            )
            await store.log_trade(order, fill, RiskDecision(allowed=True))

            assert await store.get_daily_pnl() == -4.86
            assert await store.has_filled_paper_trade_today(order.ticker)
            assert not await store.has_filled_paper_trade_today("OTHER")
            summary = await store.get_report_summary()
            assert summary["filled_contracts"] == 9
            assert summary["notional"] == 4.86
        finally:
            db_path.unlink(missing_ok=True)

    asyncio.run(run())


def test_unresolved_paper_exposure_excludes_recorded_outcomes() -> None:
    async def run() -> None:
        db_path = Path(".test_data") / f"exposure-{uuid4().hex}.sqlite3"
        store = SQLiteStore(str(db_path))
        try:
            await store.initialize()
            now = datetime.now(timezone.utc)
            await store.log_probability(
                ProbabilityEstimate(
                    ticker="RESOLVED",
                    station="TEST",
                    probability=0.70,
                    fair_yes_cents=70,
                    forecast_max_f=75,
                    adjusted_mean_f=75,
                    sigma_f=4,
                    max_data_age_minutes=5,
                    generated_at=now,
                )
            )
            resolved_order = PaperOrder("RESOLVED", "yes", 40, 2, "edge_trade")
            resolved_fill = PaperFill("RESOLVED", "yes", 2, 2, 40, 40, now)
            await store.log_trade(resolved_order, resolved_fill, RiskDecision(allowed=True))
            open_order = PaperOrder("OPEN", "yes", 30, 3, "edge_trade")
            open_fill = PaperFill("OPEN", "yes", 3, 3, 30, 30, now)
            await store.log_trade(open_order, open_fill, RiskDecision(allowed=True))

            await store.record_outcome("RESOLVED", True)

            assert await store.get_unresolved_paper_exposure() == 0.9
        finally:
            db_path.unlink(missing_ok=True)

    asyncio.run(run())


def test_unresolved_event_date_exposure_filters_by_ticker_date() -> None:
    async def run() -> None:
        db_path = Path(".test_data") / f"event-exposure-{uuid4().hex}.sqlite3"
        store = SQLiteStore(str(db_path))
        try:
            await store.initialize()
            now = datetime.now(timezone.utc)
            for ticker in ("KXHIGHNY-26MAY25-T72", "KXHIGHCHI-26MAY25-T82", "KXHIGHNY-26MAY26-T72"):
                order = PaperOrder(ticker, "yes", 50, 2, "edge_trade")
                fill = PaperFill(ticker, "yes", 2, 2, 50, 50, now)
                await store.log_trade(order, fill, RiskDecision(allowed=True))

            assert await store.get_unresolved_event_date_exposure("26MAY25") == 2.0
            assert await store.get_unresolved_event_date_exposure("26MAY26") == 1.0
        finally:
            db_path.unlink(missing_ok=True)

    asyncio.run(run())


def test_live_order_event_audit_and_counts() -> None:
    async def run() -> None:
        db_path = Path(".test_data") / f"live-events-{uuid4().hex}.sqlite3"
        store = SQLiteStore(str(db_path))
        try:
            await store.initialize()
            await store.log_live_order_event(
                event_type="submit_success",
                raw={"ok": True},
                order_id="order-1",
                client_order_id="client-1",
                ticker="TEST",
                side="yes",
                limit_price_cents=1,
                quantity=1,
                notional_dollars=0.01,
                status="submitted",
            )
            await store.log_live_order_event(
                event_type="submit_blocked",
                raw={"ok": False},
                ticker="TEST",
                status="blocked",
            )

            assert await store.count_live_submits_today() == 1
            assert await store.recent_live_rejects(10) == 1
        finally:
            db_path.unlink(missing_ok=True)

    asyncio.run(run())
