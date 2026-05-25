import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from engine.model import ProbabilityEstimate
from storage.sqlite_store import SQLiteStore


def test_record_outcome_and_calibration_report() -> None:
    async def run() -> None:
        db_path = Path(".test_data") / f"calibration-{uuid4().hex}.sqlite3"
        store = SQLiteStore(str(db_path))
        try:
            await store.initialize()
            await store.log_probability(
                ProbabilityEstimate(
                    ticker="KXHIGHNY-26MAY25-T72",
                    station="KNYC",
                    probability=0.7,
                    fair_yes_cents=70,
                    forecast_max_f=70,
                    adjusted_mean_f=70,
                    sigma_f=3,
                    generated_at=datetime.now(timezone.utc),
                    max_data_age_minutes=5,
                )
            )

            assert await store.record_outcome("KXHIGHNY-26MAY25-T72", True) == 1
            assert await store.record_outcome("KXHIGHNY-26MAY25-T72", False) == -1
            report = await store.get_calibration_report()

            assert report["summary"]["count"] == 1
            assert round(report["summary"]["avg_brier"], 2) == 0.09
            assert report["by_station"][0]["station"] == "KNYC"
        finally:
            db_path.unlink(missing_ok=True)

    asyncio.run(run())
