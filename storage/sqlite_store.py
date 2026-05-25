from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from data_ingestors.kalshi_client import MarketOrderBook
from data_ingestors.metar_client import MetarObservation
from data_ingestors.noaa_client import HourlyForecast
from engine.execution_engine import PaperFill, PaperOrder
from engine.model import ProbabilityEstimate
from engine.risk_manager import RiskDecision


class SQLiteStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS forecasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    forecast_max_f REAL,
                    raw_json TEXT NOT NULL,
                    inserted_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    temperature_f REAL,
                    raw_text TEXT,
                    raw_json TEXT NOT NULL,
                    inserted_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    best_yes_bid_cents INTEGER,
                    best_yes_ask_cents INTEGER,
                    spread_cents INTEGER,
                    raw_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS probabilities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    station TEXT NOT NULL,
                    probability REAL NOT NULL,
                    fair_yes_cents INTEGER NOT NULL,
                    forecast_max_f REAL NOT NULL,
                    adjusted_mean_f REAL NOT NULL,
                    sigma_f REAL NOT NULL,
                    max_data_age_minutes REAL NOT NULL,
                    forecast_age_minutes REAL,
                    metar_age_minutes REAL,
                    threshold_f REAL,
                    comparison TEXT,
                    upper_f REAL,
                    generated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    limit_price_cents INTEGER NOT NULL,
                    requested_quantity INTEGER NOT NULL,
                    filled_quantity INTEGER NOT NULL,
                    average_price_cents REAL,
                    risk_allowed INTEGER NOT NULL,
                    risk_reasons TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS calibration_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    station TEXT NOT NULL,
                    probability REAL NOT NULL,
                    outcome INTEGER NOT NULL,
                    brier_score REAL NOT NULL,
                    resolved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS live_order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    order_id TEXT,
                    client_order_id TEXT,
                    ticker TEXT,
                    side TEXT,
                    limit_price_cents INTEGER,
                    quantity INTEGER,
                    notional_dollars REAL,
                    status TEXT,
                    message TEXT,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                """
            )
            await _dedupe_calibration_metrics(db)
            await _ensure_column(db, "probabilities", "forecast_age_minutes", "REAL")
            await _ensure_column(db, "probabilities", "metar_age_minutes", "REAL")
            await _ensure_column(db, "probabilities", "threshold_f", "REAL")
            await _ensure_column(db, "probabilities", "comparison", "TEXT")
            await _ensure_column(db, "probabilities", "upper_f", "REAL")
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_calibration_metrics_ticker
                ON calibration_metrics(ticker)
                """
            )
            await db.commit()

    async def log_forecast(self, forecast: HourlyForecast) -> None:
        await self._execute(
            """
            INSERT INTO forecasts (station, generated_at, forecast_max_f, raw_json, inserted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                forecast.station,
                _dt(forecast.generated_at),
                forecast.forecast_max_f,
                json.dumps(forecast.raw),
                _now(),
            ),
        )

    async def log_observation(self, observation: MetarObservation | None) -> None:
        if observation is None:
            return
        await self._execute(
            """
            INSERT INTO observations (station, observed_at, temperature_f, raw_text, raw_json, inserted_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                observation.station,
                _dt(observation.observed_at),
                observation.temperature_f,
                observation.raw_text,
                json.dumps(observation.raw),
                _now(),
            ),
        )

    async def log_orderbook(self, orderbook: MarketOrderBook) -> None:
        raw = {
            "yes": [[level.price_cents, level.quantity] for level in orderbook.yes],
            "no": [[level.price_cents, level.quantity] for level in orderbook.no],
        }
        await self._execute(
            """
            INSERT INTO market_snapshots
            (ticker, captured_at, best_yes_bid_cents, best_yes_ask_cents, spread_cents, raw_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                orderbook.ticker,
                _dt(orderbook.captured_at),
                orderbook.best_yes_bid_cents,
                orderbook.best_yes_ask_cents,
                orderbook.spread_cents,
                json.dumps(raw),
            ),
        )

    async def log_probability(self, estimate: ProbabilityEstimate) -> None:
        await self._execute(
            """
            INSERT INTO probabilities
            (ticker, station, probability, fair_yes_cents, forecast_max_f, adjusted_mean_f, sigma_f,
             max_data_age_minutes, forecast_age_minutes, metar_age_minutes, threshold_f, comparison, upper_f, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                estimate.ticker,
                estimate.station,
                estimate.probability,
                estimate.fair_yes_cents,
                estimate.forecast_max_f,
                estimate.adjusted_mean_f,
                estimate.sigma_f,
                estimate.max_data_age_minutes,
                estimate.forecast_age_minutes,
                estimate.metar_age_minutes,
                estimate.threshold_f,
                estimate.comparison,
                estimate.upper_f,
                _dt(estimate.generated_at),
            ),
        )

    async def log_trade(self, order: PaperOrder, fill: PaperFill, decision: RiskDecision) -> None:
        await self._execute(
            """
            INSERT INTO paper_trades
            (ticker, side, limit_price_cents, requested_quantity, filled_quantity, average_price_cents,
             risk_allowed, risk_reasons, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.ticker,
                order.side,
                order.limit_price_cents,
                order.quantity,
                fill.filled_quantity,
                fill.average_price_cents,
                int(decision.allowed),
                json.dumps(decision.reasons),
                order.reason,
                _dt(fill.created_at),
            ),
        )

    async def log_live_order_event(
        self,
        *,
        event_type: str,
        raw: dict[str, Any],
        order_id: str | None = None,
        client_order_id: str | None = None,
        ticker: str | None = None,
        side: str | None = None,
        limit_price_cents: int | None = None,
        quantity: int | None = None,
        notional_dollars: float | None = None,
        status: str | None = None,
        message: str | None = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO live_order_events
            (event_type, order_id, client_order_id, ticker, side, limit_price_cents, quantity,
             notional_dollars, status, message, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                order_id,
                client_order_id,
                ticker,
                side,
                limit_price_cents,
                quantity,
                notional_dollars,
                status,
                message,
                json.dumps(raw),
                _now(),
            ),
        )

    async def count_live_submits_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                """
                SELECT COUNT(*)
                FROM live_order_events
                WHERE event_type = 'submit_success'
                  AND substr(created_at, 1, 10) = ?
                """,
                (today,),
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0] or 0)

    async def recent_live_rejects(self, minutes: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                """
                SELECT COUNT(*)
                FROM live_order_events
                WHERE event_type IN ('submit_blocked', 'submit_error')
                  AND created_at >= ?
                """,
                (cutoff,),
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0] or 0)

    async def get_daily_pnl(self) -> float:
        # Open-position mark-to-market is intentionally conservative until settlement integration is added.
        today = datetime.now(timezone.utc).date().isoformat()
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                """
                SELECT COALESCE(-SUM(filled_quantity * average_price_cents / 100.0), 0)
                FROM paper_trades
                WHERE substr(created_at, 1, 10) = ? AND filled_quantity > 0
                """,
                (today,),
            ) as cursor:
                row = await cursor.fetchone()
                return float(row[0] or 0)

    async def get_market_exposure(self, ticker: str) -> float:
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                """
                SELECT COALESCE(SUM(filled_quantity * average_price_cents / 100.0), 0)
                FROM paper_trades
                WHERE ticker = ? AND filled_quantity > 0
                """,
                (ticker,),
            ) as cursor:
                row = await cursor.fetchone()
                return float(row[0] or 0)

    async def get_unresolved_paper_exposure(self) -> float:
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                """
                SELECT COALESCE(SUM(pt.filled_quantity * pt.average_price_cents / 100.0), 0)
                FROM paper_trades pt
                LEFT JOIN calibration_metrics c ON c.ticker = pt.ticker
                WHERE pt.filled_quantity > 0
                  AND c.ticker IS NULL
                """
            ) as cursor:
                row = await cursor.fetchone()
                return float(row[0] or 0)

    async def get_unresolved_event_date_exposure(self, event_date: str) -> float:
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                """
                SELECT COALESCE(SUM(pt.filled_quantity * pt.average_price_cents / 100.0), 0)
                FROM paper_trades pt
                LEFT JOIN calibration_metrics c ON c.ticker = pt.ticker
                WHERE pt.filled_quantity > 0
                  AND c.ticker IS NULL
                  AND pt.ticker LIKE ?
                """,
                (f"%-{event_date}-%",),
            ) as cursor:
                row = await cursor.fetchone()
                return float(row[0] or 0)

    async def has_filled_paper_trade_today(self, ticker: str) -> bool:
        today = datetime.now(timezone.utc).date().isoformat()
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM paper_trades
                    WHERE ticker = ?
                      AND substr(created_at, 1, 10) = ?
                      AND filled_quantity > 0
                )
                """,
                (ticker, today),
            ) as cursor:
                row = await cursor.fetchone()
                return bool(row[0])

    async def get_report_rows(self, limit: int = 20) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    pt.created_at,
                    pt.ticker,
                    pt.side,
                    pt.limit_price_cents,
                    pt.requested_quantity,
                    pt.filled_quantity,
                    pt.average_price_cents,
                    pt.risk_allowed,
                    pt.risk_reasons,
                    p.station,
                    p.probability,
                    p.fair_yes_cents,
                    p.max_data_age_minutes,
                    p.forecast_age_minutes,
                    p.metar_age_minutes
                FROM paper_trades pt
                LEFT JOIN probabilities p
                  ON p.id = (
                    SELECT id FROM probabilities
                    WHERE ticker = pt.ticker AND generated_at <= pt.created_at
                    ORDER BY generated_at DESC
                    LIMIT 1
                  )
                ORDER BY pt.id DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def get_report_summary(self) -> dict[str, Any]:
        async with aiosqlite.connect(self._path) as db:
            daily_pnl = await self.get_daily_pnl()
            async with db.execute(
                """
                SELECT
                    COUNT(*) AS decisions,
                    COALESCE(SUM(risk_allowed), 0) AS allowed,
                    COALESCE(SUM(filled_quantity), 0) AS filled_contracts,
                    COALESCE(SUM(filled_quantity * average_price_cents / 100.0), 0) AS notional
                FROM paper_trades
                """
            ) as cursor:
                row = await cursor.fetchone()
                return {
                    "decisions": int(row[0] or 0),
                    "allowed": int(row[1] or 0),
                    "filled_contracts": int(row[2] or 0),
                    "notional": float(row[3] or 0),
                    "daily_pnl": daily_pnl,
                }

    async def reset_paper_history(self) -> dict[str, int]:
        tables = [
            "paper_trades",
            "probabilities",
            "market_snapshots",
            "forecasts",
            "observations",
            "calibration_metrics",
        ]
        deleted: dict[str, int] = {}
        async with aiosqlite.connect(self._path) as db:
            for table in tables:
                cursor = await db.execute(f"DELETE FROM {table}")
                deleted[table] = cursor.rowcount if cursor.rowcount is not None else 0
            await db.commit()
        return deleted

    async def record_outcome(self, ticker: str, outcome: bool) -> int:
        async with aiosqlite.connect(self._path) as db:
            async with db.execute("SELECT 1 FROM calibration_metrics WHERE ticker = ? LIMIT 1", (ticker,)) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                return -1
            async with db.execute(
                """
                SELECT ticker, station, probability
                FROM probabilities
                WHERE ticker = ?
                ORDER BY generated_at DESC
                LIMIT 1
                """,
                (ticker,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return 0
            probability = float(row[2])
            brier = (probability - float(outcome)) ** 2
            await db.execute(
                """
                INSERT INTO calibration_metrics
                (ticker, station, probability, outcome, brier_score, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row[0], row[1], probability, int(outcome), brier, _now()),
            )
            await db.commit()
            return 1

    async def get_calibration_report(self) -> dict[str, Any]:
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                """
                SELECT
                    COUNT(*) AS count,
                    COALESCE(AVG(brier_score), 0) AS avg_brier,
                    COALESCE(AVG(probability), 0) AS avg_probability,
                    COALESCE(AVG(outcome), 0) AS outcome_rate
                FROM calibration_metrics
                """
            ) as cursor:
                summary = await cursor.fetchone()
            async with db.execute(
                """
                SELECT
                    station,
                    COUNT(*) AS count,
                    AVG(brier_score) AS avg_brier,
                    AVG(probability) AS avg_probability,
                    AVG(outcome) AS outcome_rate
                FROM calibration_metrics
                GROUP BY station
                ORDER BY station
                """
            ) as cursor:
                station_rows = await cursor.fetchall()
            async with db.execute(
                """
                SELECT ticker, station, probability, outcome, brier_score, resolved_at
                FROM calibration_metrics
                ORDER BY id DESC
                LIMIT 10
                """
            ) as cursor:
                latest = await cursor.fetchall()

        return {
            "summary": {
                "count": int(summary[0] or 0),
                "avg_brier": float(summary[1] or 0),
                "avg_probability": float(summary[2] or 0),
                "outcome_rate": float(summary[3] or 0),
            },
            "by_station": [
                {
                    "station": row[0],
                    "count": int(row[1]),
                    "avg_brier": float(row[2]),
                    "avg_probability": float(row[3]),
                    "outcome_rate": float(row[4]),
                }
                for row in station_rows
            ],
            "latest": [
                {
                    "ticker": row[0],
                    "station": row[1],
                    "probability": float(row[2]),
                    "outcome": bool(row[3]),
                    "brier_score": float(row[4]),
                    "resolved_at": row[5],
                }
                for row in latest
            ],
        }

    async def get_unresolved_probability_tickers(self, limit: int = 100) -> list[str]:
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                """
                SELECT DISTINCT p.ticker
                FROM probabilities p
                LEFT JOIN calibration_metrics c ON c.ticker = p.ticker
                WHERE c.ticker IS NULL
                ORDER BY p.generated_at DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [str(row[0]) for row in rows]

    async def get_latest_allowed_paper_trade(self) -> dict[str, Any] | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT ticker, side, limit_price_cents, requested_quantity, filled_quantity,
                       average_price_cents, created_at
                FROM paper_trades
                WHERE risk_allowed = 1
                ORDER BY id DESC
                LIMIT 1
                """
            ) as cursor:
                row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def get_open_paper_positions(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    ticker,
                    SUM(filled_quantity) AS quantity,
                    SUM(filled_quantity * average_price_cents) / SUM(filled_quantity) AS average_entry_cents,
                    SUM(filled_quantity * average_price_cents / 100.0) AS cost_dollars
                FROM paper_trades
                WHERE filled_quantity > 0
                GROUP BY ticker
                ORDER BY ticker
                """
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_filled_paper_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    id,
                    created_at,
                    ticker,
                    side,
                    limit_price_cents,
                    requested_quantity,
                    filled_quantity,
                    average_price_cents,
                    risk_allowed,
                    risk_reasons,
                    reason
                FROM paper_trades
                WHERE filled_quantity > 0
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_paper_performance(self) -> dict[str, Any]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    COUNT(*) AS decisions,
                    COALESCE(SUM(CASE WHEN risk_allowed = 1 THEN 1 ELSE 0 END), 0) AS allowed,
                    COALESCE(SUM(CASE WHEN filled_quantity > 0 THEN 1 ELSE 0 END), 0) AS filled_trades,
                    COALESCE(SUM(filled_quantity), 0) AS filled_contracts,
                    COALESCE(SUM(filled_quantity * average_price_cents / 100.0), 0) AS paper_cost
                FROM paper_trades
                """
            ) as cursor:
                summary = dict(await cursor.fetchone())

            async with db.execute(
                """
                SELECT pt.risk_reasons
                FROM paper_trades pt
                WHERE pt.risk_allowed = 0
                """
            ) as cursor:
                reason_rows = await cursor.fetchall()

            async with db.execute(
                """
                SELECT
                    AVG(p.probability - (pt.limit_price_cents / 100.0)) AS avg_edge,
                    MIN(p.probability - (pt.limit_price_cents / 100.0)) AS min_edge,
                    MAX(p.probability - (pt.limit_price_cents / 100.0)) AS max_edge
                FROM paper_trades pt
                JOIN probabilities p
                  ON p.id = (
                    SELECT id FROM probabilities
                    WHERE ticker = pt.ticker AND generated_at <= pt.created_at
                    ORDER BY generated_at DESC
                    LIMIT 1
                  )
                WHERE pt.risk_allowed = 1
                  AND pt.filled_quantity > 0
                """
            ) as cursor:
                edge_row = dict(await cursor.fetchone())

            async with db.execute(
                """
                SELECT
                    COUNT(*) AS resolved_trades,
                    COALESCE(SUM(CASE WHEN c.outcome = 1 THEN 1 ELSE 0 END), 0) AS wins,
                    COALESCE(SUM(CASE WHEN c.outcome = 0 THEN 1 ELSE 0 END), 0) AS losses,
                    COALESCE(SUM(
                        CASE
                            WHEN c.outcome = 1 THEN pt.filled_quantity * (1 - pt.average_price_cents / 100.0)
                            ELSE -pt.filled_quantity * pt.average_price_cents / 100.0
                        END
                    ), 0) AS realized_pnl
                FROM paper_trades pt
                JOIN calibration_metrics c ON c.ticker = pt.ticker
                WHERE pt.filled_quantity > 0
                """
            ) as cursor:
                realized = dict(await cursor.fetchone())

        skip_reasons: dict[str, int] = {}
        for row in reason_rows:
            reasons = json.loads(row["risk_reasons"] or "[]")
            for reason in reasons or ["unknown"]:
                skip_reasons[str(reason)] = skip_reasons.get(str(reason), 0) + 1

        return {
            "summary": {
                "decisions": int(summary["decisions"] or 0),
                "allowed": int(summary["allowed"] or 0),
                "filled_trades": int(summary["filled_trades"] or 0),
                "filled_contracts": int(summary["filled_contracts"] or 0),
                "paper_cost": float(summary["paper_cost"] or 0),
            },
            "edge": {
                "avg": float(edge_row["avg_edge"]) if edge_row["avg_edge"] is not None else None,
                "min": float(edge_row["min_edge"]) if edge_row["min_edge"] is not None else None,
                "max": float(edge_row["max_edge"]) if edge_row["max_edge"] is not None else None,
            },
            "realized": {
                "resolved_trades": int(realized["resolved_trades"] or 0),
                "wins": int(realized["wins"] or 0),
                "losses": int(realized["losses"] or 0),
                "realized_pnl": float(realized["realized_pnl"] or 0),
            },
            "skip_reasons": sorted(skip_reasons.items(), key=lambda item: (-item[1], item[0])),
        }

    async def get_research_export_rows(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    pt.created_at,
                    pt.ticker,
                    p.station,
                    p.probability,
                    p.fair_yes_cents,
                    p.forecast_max_f,
                    p.adjusted_mean_f,
                    p.sigma_f,
                    p.max_data_age_minutes,
                    p.forecast_age_minutes,
                    p.metar_age_minutes,
                    p.threshold_f,
                    p.comparison,
                    p.upper_f,
                    ms.best_yes_bid_cents,
                    ms.best_yes_ask_cents,
                    ms.spread_cents,
                    pt.side,
                    pt.limit_price_cents,
                    pt.requested_quantity,
                    pt.filled_quantity,
                    pt.average_price_cents,
                    pt.risk_allowed,
                    pt.risk_reasons,
                    pt.reason,
                    c.outcome,
                    c.brier_score,
                    c.resolved_at
                FROM paper_trades pt
                LEFT JOIN probabilities p
                  ON p.id = (
                    SELECT id FROM probabilities
                    WHERE ticker = pt.ticker AND generated_at <= pt.created_at
                    ORDER BY generated_at DESC
                    LIMIT 1
                  )
                LEFT JOIN market_snapshots ms
                  ON ms.id = (
                    SELECT id FROM market_snapshots
                    WHERE ticker = pt.ticker AND captured_at <= pt.created_at
                    ORDER BY captured_at DESC
                    LIMIT 1
                  )
                LEFT JOIN calibration_metrics c ON c.ticker = pt.ticker
                ORDER BY pt.created_at
                """
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_provider_snapshot_rows(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT station, generated_at, forecast_max_f, raw_json, inserted_at
                FROM forecasts
                ORDER BY generated_at
                """
            ) as cursor:
                rows = await cursor.fetchall()

        snapshots: list[dict[str, Any]] = []
        for row in rows:
            raw = json.loads(row["raw_json"] or "{}")
            consensus = raw.get("weather_bot_consensus") if isinstance(raw, dict) else None
            if not isinstance(consensus, dict):
                continue
            base = {
                "station": row["station"],
                "generated_at": row["generated_at"],
                "inserted_at": row["inserted_at"],
                "noaa_max_f": consensus.get("noaa_max_f"),
                "consensus_max_f": consensus.get("consensus_max_f"),
            }
            snapshots.append(
                {
                    **base,
                    "provider": "weather_bot_consensus",
                    "provider_max_f": consensus.get("consensus_max_f"),
                    "weight": 1.0,
                }
            )
            for source in consensus.get("external_sources") or []:
                if not isinstance(source, dict):
                    continue
                snapshots.append(
                    {
                        **base,
                        "provider": source.get("source"),
                        "provider_max_f": source.get("max_f"),
                        "weight": source.get("weight"),
                    }
                )
        return snapshots

    async def get_provider_replay_rows(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    c.ticker,
                    c.station,
                    c.outcome,
                    c.brier_score,
                    p.probability,
                    p.threshold_f,
                    p.comparison,
                    p.upper_f,
                    p.generated_at AS probability_generated_at,
                    f.raw_json AS forecast_raw_json,
                    f.generated_at AS forecast_generated_at
                FROM calibration_metrics c
                JOIN probabilities p
                  ON p.id = (
                    SELECT id FROM probabilities
                    WHERE ticker = c.ticker
                    ORDER BY generated_at DESC
                    LIMIT 1
                  )
                LEFT JOIN forecasts f
                  ON f.id = (
                    SELECT id FROM forecasts
                    WHERE station = p.station
                      AND generated_at <= p.generated_at
                    ORDER BY generated_at DESC
                    LIMIT 1
                  )
                WHERE p.threshold_f IS NOT NULL
                  AND p.comparison IS NOT NULL
                ORDER BY p.generated_at
                """
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def _execute(self, sql: str, params: tuple[Any, ...]) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(sql, params)
            await db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


async def _dedupe_calibration_metrics(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        DELETE FROM calibration_metrics
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM calibration_metrics
            GROUP BY ticker
        )
        """
    )


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str) -> None:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        columns = {str(row[1]) for row in await cursor.fetchall()}
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
