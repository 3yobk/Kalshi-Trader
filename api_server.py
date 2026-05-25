"""
api_server.py  –  FastAPI backend for the forecast-guard-watch dashboard.

Run:  uvicorn api_server:app --host 127.0.0.1 --port 8000 --reload

All data is read from the same SQLite store and Kalshi clients that main.py uses.
No live orders are placed from this server (cancel-order is the only mutating call).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import load_config
from data_ingestors.kalshi_auth_client import KalshiAuthClient
from data_ingestors.kalshi_client import KalshiClient
from data_ingestors.metar_client import MetarClient
from data_ingestors.noaa_client import NoaaClient
from data_ingestors.open_meteo_client import OpenMeteoClient
from storage.sqlite_store import SQLiteStore

logger = logging.getLogger("api_server")

# ---------------------------------------------------------------------------
# Simple in-process cache for expensive endpoints
# ---------------------------------------------------------------------------

import time as _time

class _Cache:
    def __init__(self, ttl_seconds: float):
        self._ttl = ttl_seconds
        self._value: Any = None
        self._ts: float = 0.0
        self._lock = asyncio.Lock()

    def fresh(self) -> bool:
        return self._value is not None and (_time.monotonic() - self._ts) < self._ttl

    async def get_or_set(self, fn):  # type: ignore[override]
        async with self._lock:
            if self.fresh():
                return self._value
            self._value = await fn()
            self._ts = _time.monotonic()
            return self._value

_data_health_cache = _Cache(ttl_seconds=120)   # refresh at most every 2 min

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Kalshi Weather Bot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_store() -> SQLiteStore:
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    return store


def _make_auth_client() -> KalshiAuthClient | None:
    settings, _ = load_config()
    key_id = settings.kalshi_api_key_id
    key_path = settings.kalshi_private_key_path
    if not key_id or not key_path:
        return None
    try:
        return KalshiAuthClient(
            base_url=settings.kalshi_base_url,
            api_key_id=key_id,
            private_key_path=key_path,
        )
    except Exception as exc:
        logger.warning("Could not create auth client: %s", exc)
        return None


def _open_orders(payload: dict[str, Any]) -> list[dict[str, Any]]:
    orders = payload.get("orders") or []
    return [o for o in orders if str(o.get("status", "")).lower() in {"resting", "open", "pending"}]


def _nonzero_positions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    positions = payload.get("market_positions") or payload.get("positions") or []
    result = []
    for p in positions:
        qty = p.get("position") or p.get("yes_count") or p.get("quantity") or 0
        try:
            if float(qty) != 0:
                result.append(p)
        except (TypeError, ValueError):
            result.append(p)
    return result


def _exposure_event_date(ticker: str) -> str:
    parts = ticker.split("-")
    return parts[1] if len(parts) > 1 else "unknown"


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def get_status() -> dict[str, Any]:
    settings, config = load_config()
    store = await _get_store()

    paper = await store.get_report_summary()
    unresolved_exposure = await store.get_unresolved_paper_exposure()
    latest_trade = await store.get_latest_allowed_paper_trade()

    auth_status = "MISSING"
    balance_dollars = 0.0
    positions_count = 0
    orders_count = 0

    client = _make_auth_client()
    if client is not None:
        try:
            auth = await client.auth_check()
            auth_status = "OK" if auth.ok else "FAIL"
            if auth.ok:
                balance_payload = await client.get_balance()
                positions_payload = await client.get_positions(limit=100)
                orders_payload = await client.get_orders(limit=100)
                balance_cents = int(
                    balance_payload.get("balance")
                    or balance_payload.get("portfolio_value")
                    or 0
                )
                balance_dollars = balance_cents / 100
                positions_count = len(_nonzero_positions(positions_payload))
                orders_count = len(_open_orders(orders_payload))
        except Exception as exc:
            logger.warning("Auth client error in /api/status: %s", exc)
            auth_status = "FAIL"
        finally:
            await client.close()

    return {
        "mode": settings.bot_env,
        "paper_only": config.bot.paper_only,
        "kalshi_auth": auth_status,
        "balance_dollars": balance_dollars,
        "open_live_orders": orders_count,
        "live_positions": positions_count,
        "paper_decisions": paper.get("decisions", 0),
        "allowed_paper_trades": paper.get("allowed", 0),
        "unresolved_exposure_dollars": unresolved_exposure,
        "last_refresh": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /api/decisions
# ---------------------------------------------------------------------------

@app.get("/api/decisions")
async def get_decisions(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    store = await _get_store()
    rows = await store.get_report_rows(limit=limit)
    result = []
    for r in rows:
        risk_reasons_raw = r.get("risk_reasons") or "[]"
        if isinstance(risk_reasons_raw, str):
            import json as _json
            try:
                risk_reasons_raw = _json.loads(risk_reasons_raw)
            except Exception:
                risk_reasons_raw = [risk_reasons_raw] if risk_reasons_raw else []
        result.append({
            "created_at": r.get("created_at") or "",
            "ticker": r.get("ticker", ""),
            "station": r.get("station", "") or "",
            "probability": float(r.get("probability") or 0),
            "best_yes_ask_cents": int(r.get("limit_price_cents") or 0),
            "edge": round(float(r.get("probability") or 0) - float(r.get("limit_price_cents") or 0) / 100, 4),
            "risk_allowed": bool(r.get("risk_allowed")),
            "risk_reasons": risk_reasons_raw if isinstance(risk_reasons_raw, list) else [],
            "forecast_age_minutes": float(r.get("forecast_age_minutes") or 0),
            "metar_age_minutes": float(r.get("metar_age_minutes") or 0),
            "spread_cents": 0,
            "liquidity": 0,
        })
    return result


# ---------------------------------------------------------------------------
# GET /api/performance
# ---------------------------------------------------------------------------

@app.get("/api/performance")
async def get_performance() -> dict[str, Any]:
    store = await _get_store()
    perf = await store.get_paper_performance()
    summary = perf.get("summary", {})
    edge = perf.get("edge", {})
    realized = perf.get("realized", {})
    resolved = int(realized.get("resolved_trades") or 0)
    wins = int(realized.get("wins") or 0)
    return {
        "trades": int(summary.get("filled_trades") or 0),
        "win_rate": wins / resolved if resolved else 0.0,
        "avg_edge": float(edge.get("avg") or 0),
        "realized_pnl": float(realized.get("realized_pnl") or 0),
        "best_trade": float(realized.get("best_trade") or 0),
        "worst_trade": float(realized.get("worst_trade") or 0),
    }


# ---------------------------------------------------------------------------
# GET /api/ledger
# ---------------------------------------------------------------------------

@app.get("/api/ledger")
async def get_ledger(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    store = await _get_store()
    trades = await store.get_filled_paper_trades(limit=limit)
    result = []
    for t in trades:
        qty = int(t.get("filled_quantity") or 0)
        price_cents = float(t.get("average_price_cents") or 0)
        cost = qty * price_cents / 100
        realized = t.get("realized_pnl")
        result.append({
            "ts": t.get("created_at") or "",
            "ticker": t.get("ticker", ""),
            "side": str(t.get("side", "YES")).upper(),
            "qty": qty,
            "price_cents": price_cents,
            "cost_dollars": cost,
            "realized_pnl": float(realized) if realized is not None else None,
        })
    return result


# ---------------------------------------------------------------------------
# GET /api/mtm
# ---------------------------------------------------------------------------

@app.get("/api/mtm")
async def get_mtm() -> dict[str, Any]:
    settings, _ = load_config()
    store = await _get_store()
    positions = await store.get_open_paper_positions()

    kalshi = KalshiClient(settings.kalshi_base_url)
    total_cost = 0.0
    total_value = 0.0
    mtm_positions = []
    try:
        for pos in positions:
            ticker = pos["ticker"]
            qty = int(pos.get("quantity") or 0)
            avg = float(pos.get("average_entry_cents") or 0)
            cost = float(pos.get("cost_dollars") or qty * avg / 100)
            try:
                ob = await kalshi.get_orderbook(ticker)
                bid = ob.best_yes_bid_cents or 0
            except Exception:
                bid = 0
            value = qty * bid / 100
            total_cost += cost
            total_value += value
            mtm_positions.append({
                "ticker": ticker,
                "qty": qty,
                "avg_price_cents": avg,
                "mark_cents": bid,
                "cost": cost,
                "value": value,
                "unrealized": value - cost,
            })
    finally:
        await kalshi.close()

    perf = await store.get_paper_performance()
    realized_pnl = float(perf.get("realized", {}).get("realized_pnl") or 0)

    return {
        "total_cost": total_cost,
        "total_value": total_value,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": total_value - total_cost,
        "positions": mtm_positions,
    }


# ---------------------------------------------------------------------------
# GET /api/exposure
# ---------------------------------------------------------------------------

@app.get("/api/exposure")
async def get_exposure() -> dict[str, Any]:
    _, config = load_config()
    store = await _get_store()
    unresolved_total = await store.get_unresolved_paper_exposure()
    positions = await store.get_open_paper_positions()

    by_event: dict[str, float] = {}
    for pos in positions:
        ticker = str(pos.get("ticker", ""))
        cost = float(pos.get("cost_dollars") or 0)
        event_date = _exposure_event_date(ticker)
        by_event[event_date] = by_event.get(event_date, 0) + cost

    cap = config.risk.max_event_date_exposure_dollars
    return {
        "unresolved_total": round(unresolved_total, 2),
        "by_event_date": [
            {"date": d, "exposure": round(v, 2), "cap": cap}
            for d, v in sorted(by_event.items())
        ],
    }


# ---------------------------------------------------------------------------
# GET /api/calibration
# ---------------------------------------------------------------------------

@app.get("/api/calibration")
async def get_calibration() -> dict[str, Any]:
    store = await _get_store()
    report = await store.get_calibration_report()
    summary = report.get("summary", {})
    by_station = report.get("by_station", [])
    latest = report.get("latest", [])

    # Build probability buckets from resolved outcomes
    bucket_edges = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    buckets_data: dict[tuple[float, float], list[tuple[float, bool]]] = {}
    wins = 0
    losses = 0
    for row in latest:
        p = float(row.get("probability") or 0)
        o = bool(row.get("outcome"))
        if o:
            wins += 1
        else:
            losses += 1
        lo, hi = 0.0, 1.0
        for i in range(len(bucket_edges) - 1):
            if bucket_edges[i] <= p < bucket_edges[i + 1]:
                lo, hi = bucket_edges[i], bucket_edges[i + 1]
                break
        buckets_data.setdefault((lo, hi), []).append((p, o))

    buckets = []
    for (lo, hi), items in sorted(buckets_data.items()):
        n = len(items)
        predicted = sum(p for p, _ in items) / n if n else (lo + hi) / 2
        actual = sum(1 for _, o in items if o) / n if n else 0
        buckets.append({"lo": lo, "hi": hi, "predicted": round(predicted, 3), "actual": round(actual, 3), "n": n})

    return {
        "brier": float(summary.get("avg_brier") or 0),
        "resolved": int(summary.get("count") or 0),
        "wins": wins,
        "losses": losses,
        "stations": [
            {"station": r.get("station", ""), "brier": float(r.get("avg_brier") or 0), "n": int(r.get("count") or 0)}
            for r in by_station
        ],
        "buckets": buckets,
    }


# ---------------------------------------------------------------------------
# GET /api/providers
# ---------------------------------------------------------------------------

@app.get("/api/providers")
async def get_providers() -> list[dict[str, Any]]:
    settings, config = load_config()
    m = config.model
    configured_map = {
        "noaa": True,
        "open_meteo": True,
        "tomorrow_io": bool(settings.tomorrow_api_key),
        "visual_crossing": bool(settings.visual_crossing_api_key),
        "weatherapi": bool(settings.weatherapi_key),
        "meteostat": bool(settings.meteostat_rapidapi_key),
        "climatology": True,
    }
    weights = {
        "noaa": 1.0,
        "open_meteo": m.open_meteo_weight,
        "tomorrow_io": m.tomorrow_weight,
        "visual_crossing": m.visual_crossing_weight,
        "weatherapi": m.weatherapi_weight,
        "meteostat": m.meteostat_weight,
        "climatology": m.climatology_weight,
    }
    # Pull last_success and replay_reliability from stored snapshot rows
    store = await _get_store()
    snaps = await store.get_provider_snapshot_rows()
    last_success_map: dict[str, str] = {}
    for s in snaps:
        prov = str(s.get("provider") or "")
        ts = str(s.get("generated_at") or "")
        if prov and ts:
            if prov not in last_success_map or ts > last_success_map[prov]:
                last_success_map[prov] = ts

    # Replay reliability from replay rows
    replay_rows = await store.get_provider_replay_rows()
    replay_map: dict[str, list[float]] = {}
    for r in replay_rows:
        prov = str(r.get("provider") or r.get("source") or "")
        err = r.get("abs_error_f")
        if prov and err is not None:
            replay_map.setdefault(prov, []).append(float(err))
    replay_reliability: dict[str, float] = {
        p: 1.0 / (1.0 + sum(v) / len(v)) for p, v in replay_map.items() if v
    }

    return [
        {
            "name": name,
            "configured": configured_map[name],
            "weight": weights[name],
            "last_success": last_success_map.get(name) or last_success_map.get(name.replace("_", " ")),
            "replay_reliability": replay_reliability.get(name),
        }
        for name in weights
    ]


# ---------------------------------------------------------------------------
# GET /api/providers/snapshots
# ---------------------------------------------------------------------------

@app.get("/api/providers/snapshots")
async def get_provider_snapshots() -> list[dict[str, Any]]:
    store = await _get_store()
    rows = await store.get_provider_snapshot_rows()
    return [
        {
            "station": r.get("station", ""),
            "generated_at": r.get("generated_at") or "",
            "provider": r.get("provider") or "",
            "provider_max_f": r.get("provider_max_f"),
            "weight": float(r.get("weight") or 0),
            "noaa_max_f": r.get("noaa_max_f"),
            "consensus_max_f": r.get("consensus_max_f"),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# GET /api/data-health
# ---------------------------------------------------------------------------

@app.get("/api/data-health")
async def get_data_health() -> dict[str, Any]:
    return await _data_health_cache.get_or_set(_fetch_data_health)


async def _fetch_data_health() -> dict[str, Any]:
    settings, config = load_config()
    noaa = NoaaClient()
    metar = MetarClient()
    open_meteo = OpenMeteoClient()

    stale_stations: list[str] = []
    failed_providers: list[str] = []
    noaa_age_min = 0.0
    metar_age_min = 0.0
    forecast_limit = config.risk.stale_forecast_minutes
    metar_limit = config.risk.stale_metar_minutes

    try:
        for station in config.bot.supported_stations[:4]:
            point = config.bot.station_points.get(station)
            if not point:
                stale_stations.append(station)
                continue
            try:
                forecast, obs = await asyncio.gather(
                    noaa.get_hourly_forecast(station, point["lat"], point["lon"]),
                    metar.get_latest(station),
                )
                noaa_age_min = max(noaa_age_min, forecast.age_minutes)
                if forecast.age_minutes > forecast_limit:
                    stale_stations.append(station)
                if obs is not None:
                    metar_age_min = max(metar_age_min, obs.age_minutes)
                    if obs.age_minutes > metar_limit:
                        failed_providers.append(f"metar:{station}")
            except Exception as exc:
                failed_providers.append(f"noaa:{station}:{exc.__class__.__name__}")
    finally:
        await noaa.close()
        await open_meteo.close()
        await metar.close()

    return {
        "noaa_age_min": round(noaa_age_min, 1),
        "metar_age_min": round(metar_age_min, 1),
        "stale_stations": stale_stations,
        "failed_providers": failed_providers,
    }


# ---------------------------------------------------------------------------
# GET /api/live/readiness
# ---------------------------------------------------------------------------

@app.get("/api/live/readiness")
async def get_live_readiness() -> dict[str, Any]:
    settings, config = load_config()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    paper_mode = settings.bot_env == "paper"
    manual_live = settings.bot_env == "live"
    add("mode is explicit", paper_mode or manual_live, f"BOT_ENV={settings.bot_env}")
    add("max trade size ≤ $5", 0 < config.risk.max_trade_dollars <= 5, f"${config.risk.max_trade_dollars:.2f}")
    add("daily loss cap ≤ $20", 0 < config.risk.max_daily_loss_dollars <= 20, f"${config.risk.max_daily_loss_dollars:.2f}")
    add("unresolved exposure cap", 0 < config.risk.max_unresolved_exposure_dollars <= 20, f"${config.risk.max_unresolved_exposure_dollars:.2f}")
    add("min edge ≥ 15%", config.risk.min_edge >= 0.15, f"{config.risk.min_edge:.2%}")
    add("private key configured", bool(settings.kalshi_private_key_path and Path(settings.kalshi_private_key_path).exists()), str(settings.kalshi_private_key_path))
    add("api key id configured", bool(settings.kalshi_api_key_id), "set" if settings.kalshi_api_key_id else "missing")

    client = _make_auth_client()
    if client is not None:
        try:
            auth = await client.auth_check()
            add("Kalshi auth check", auth.ok, f"HTTP {auth.status_code}")
        except Exception as exc:
            add("Kalshi auth check", False, str(exc))
        finally:
            await client.close()
    else:
        add("Kalshi auth check", False, "missing credentials")

    return {"ready": all(c["ok"] for c in checks), "checks": checks}


# ---------------------------------------------------------------------------
# GET /api/live/orders
# ---------------------------------------------------------------------------

@app.get("/api/live/orders")
async def get_live_orders() -> list[dict[str, Any]]:
    client = _make_auth_client()
    if client is None:
        return []
    try:
        payload = await client.get_orders(limit=100)
        return [
            {
                "order_id": o.get("order_id") or o.get("id", ""),
                "ticker": o.get("ticker", ""),
                "side": str(o.get("side", "YES")).upper(),
                "qty": int(o.get("count") or o.get("quantity") or o.get("qty") or 0),
                "price_cents": int(o.get("yes_price") or o.get("price") or o.get("limit_price") or 0),
                "status": str(o.get("status", "")),
                "created_at": o.get("created_time") or o.get("created_at") or "",
            }
            for o in _open_orders(payload)
        ]
    except Exception as exc:
        logger.warning("get_live_orders error: %s", exc)
        return []
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# GET /api/live/positions
# ---------------------------------------------------------------------------

@app.get("/api/live/positions")
async def get_live_positions() -> list[dict[str, Any]]:
    settings, _ = load_config()
    client = _make_auth_client()
    if client is None:
        return []
    try:
        payload = await client.get_positions(limit=200)
        positions = _nonzero_positions(payload)
        kalshi = KalshiClient(settings.kalshi_base_url)
        result = []
        try:
            for p in positions:
                ticker = p.get("ticker", "")
                qty = int(p.get("position") or p.get("yes_count") or p.get("quantity") or 0)
                avg = int(p.get("avg_price") or p.get("average_price") or 0)
                try:
                    ob = await kalshi.get_orderbook(ticker)
                    mark = ob.best_yes_bid_cents or 0
                except Exception:
                    mark = 0
                result.append({"ticker": ticker, "qty": qty, "avg_price_cents": avg, "mark_cents": mark})
        finally:
            await kalshi.close()
        return result
    except Exception as exc:
        logger.warning("get_live_positions error: %s", exc)
        return []
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# DELETE /api/live/orders/{order_id}
# ---------------------------------------------------------------------------

@app.delete("/api/live/orders/{order_id}")
async def cancel_live_order(order_id: str) -> dict[str, Any]:
    client = _make_auth_client()
    if client is None:
        raise HTTPException(status_code=503, detail="Kalshi auth not configured")
    try:
        result = await client.cancel_event_order(order_id)
        return {"ok": True, "order_id": order_id, "detail": result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# GET /api/risk
# ---------------------------------------------------------------------------

@app.get("/api/risk")
async def get_risk() -> dict[str, Any]:
    settings, config = load_config()
    r = config.risk
    k = config.kalshi
    return {
        "max_trade_size_dollars": r.max_trade_dollars,
        "daily_loss_cap_dollars": r.max_daily_loss_dollars,
        "unresolved_exposure_cap_dollars": r.max_unresolved_exposure_dollars,
        "event_date_exposure_cap_dollars": r.max_event_date_exposure_dollars,
        "min_edge": r.min_edge,
        "max_forecast_age_min": r.stale_forecast_minutes,
        "max_metar_age_min": r.stale_metar_minutes,
        "max_spread_cents": k.max_spread_cents,
        "min_liquidity": k.min_liquidity_contracts,
        "live_manual_order_cap_dollars": settings.live_max_order_dollars,
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# POST /api/run-once  — trigger one bot polling cycle
# ---------------------------------------------------------------------------

import subprocess
import sys

_run_lock = asyncio.Lock()
_last_run: dict[str, Any] = {"status": "never", "started_at": None, "finished_at": None, "log": []}

@app.post("/api/run-once")
async def run_once_endpoint() -> dict[str, Any]:
    if _run_lock.locked():
        return {"ok": False, "error": "A run is already in progress"}
    async with _run_lock:
        started = datetime.now(timezone.utc).isoformat()
        _last_run.update({"status": "running", "started_at": started, "finished_at": None, "log": []})
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "main.py", "--once",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            lines = stdout.decode(errors="replace").splitlines()[-60:]
            finished = datetime.now(timezone.utc).isoformat()
            ok = proc.returncode == 0
            _last_run.update({
                "status": "ok" if ok else "error",
                "started_at": started,
                "finished_at": finished,
                "log": lines,
                "exit_code": proc.returncode,
            })
            return {"ok": ok, "exit_code": proc.returncode, "log": lines}
        except asyncio.TimeoutError:
            _last_run.update({"status": "timeout", "finished_at": datetime.now(timezone.utc).isoformat()})
            return {"ok": False, "error": "Bot run timed out after 5 minutes"}
        except Exception as exc:
            _last_run.update({"status": "error", "finished_at": datetime.now(timezone.utc).isoformat(), "log": [str(exc)]})
            return {"ok": False, "error": str(exc)}


@app.get("/api/run-status")
async def run_status() -> dict[str, Any]:
    return {**_last_run, "running": _run_lock.locked()}


# ---------------------------------------------------------------------------
# GET /api/unresolved  — tickers with no recorded outcome yet
# ---------------------------------------------------------------------------

@app.get("/api/unresolved")
async def get_unresolved(limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    store = await _get_store()
    settings, config = load_config()
    tickers = await store.get_unresolved_probability_tickers(limit=limit)

    kalshi = KalshiClient(settings.kalshi_base_url)
    results = []
    try:
        for ticker in tickers:
            try:
                market = await kalshi.get_market(ticker)
                status = market.get("status") or market.get("settlement_status") or "unknown"
                result = market.get("result")
                expiration_value = market.get("expiration_value")
                close_time = market.get("close_time") or market.get("expiration_time") or ""
            except Exception:
                status = "fetch_error"
                result = None
                expiration_value = None
                close_time = ""
            results.append({
                "ticker": ticker,
                "status": status,
                "result": result,
                "expiration_value": expiration_value,
                "close_time": close_time,
            })
    finally:
        await kalshi.close()
    return results


# ---------------------------------------------------------------------------
# POST /api/record-outcome  — manually record one outcome
# ---------------------------------------------------------------------------

class RecordOutcomeRequest(BaseModel):
    ticker: str
    outcome: str  # "yes" or "no"

@app.post("/api/record-outcome")
async def record_outcome_endpoint(req: RecordOutcomeRequest) -> dict[str, Any]:
    if req.outcome not in {"yes", "no"}:
        raise HTTPException(status_code=400, detail="outcome must be 'yes' or 'no'")
    store = await _get_store()
    result = await store.record_outcome(ticker=req.ticker, outcome=req.outcome == "yes")
    if result == 1:
        return {"ok": True, "ticker": req.ticker, "outcome": req.outcome, "detail": "recorded"}
    elif result == -1:
        return {"ok": False, "ticker": req.ticker, "detail": "already recorded"}
    else:
        return {"ok": False, "ticker": req.ticker, "detail": "no probability record found for this ticker"}


# ---------------------------------------------------------------------------
# POST /api/settlement-sync  — auto-record Kalshi-confirmed settlements
# ---------------------------------------------------------------------------

@app.post("/api/settlement-sync")
async def settlement_sync_endpoint(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    store = await _get_store()
    settings, _ = load_config()
    tickers = await store.get_unresolved_probability_tickers(limit=limit)

    if not tickers:
        return {"ok": True, "recorded": 0, "skipped": 0, "previews": []}

    kalshi = KalshiClient(settings.kalshi_base_url)
    recorded = 0
    skipped = 0
    previews: list[dict[str, Any]] = []

    def _settlement_outcome(market: dict[str, Any]) -> bool | None:
        result = market.get("result")
        if isinstance(result, str):
            n = result.strip().lower()
            if n == "yes": return True
            if n == "no": return False
        value = market.get("expiration_value")
        if isinstance(value, str):
            n = value.strip().lower()
            if n in {"yes", "y", "true", "1"}: return True
            if n in {"no", "n", "false", "0"}: return False
        return None

    try:
        for ticker in tickers:
            try:
                market = await kalshi.get_market(ticker)
            except Exception as exc:
                previews.append({"ticker": ticker, "action": "error", "detail": str(exc)})
                skipped += 1
                continue

            outcome = _settlement_outcome(market)
            status = market.get("status") or market.get("settlement_status") or "unknown"

            if outcome is None:
                previews.append({"ticker": ticker, "action": "skip", "detail": f"no settlement value (status={status})"})
                skipped += 1
                continue

            result = await store.record_outcome(ticker, outcome)
            if result == 1:
                recorded += 1
                previews.append({"ticker": ticker, "action": "recorded", "outcome": "yes" if outcome else "no"})
            elif result == -1:
                skipped += 1
                previews.append({"ticker": ticker, "action": "skip", "detail": "already recorded"})
            else:
                skipped += 1
                previews.append({"ticker": ticker, "action": "skip", "detail": "no probability record"})
    finally:
        await kalshi.close()

    return {"ok": True, "recorded": recorded, "skipped": skipped, "previews": previews}


# ---------------------------------------------------------------------------
# GET /api/decisions/history  — edge + allowed-rate over time for charting
# ---------------------------------------------------------------------------

@app.get("/api/decisions/history")
async def get_decisions_history(limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
    store = await _get_store()
    rows = await store.get_report_rows(limit=limit)
    result = []
    for r in rows:
        result.append({
            "created_at": r.get("created_at") or "",
            "ticker": r.get("ticker", ""),
            "station": r.get("station") or "",
            "probability": float(r.get("probability") or 0),
            "edge": round(float(r.get("probability") or 0) - float(r.get("limit_price_cents") or 0) / 100, 4),
            "risk_allowed": bool(r.get("risk_allowed")),
            "forecast_age_minutes": float(r.get("forecast_age_minutes") or 0),
        })
    return list(reversed(result))  # oldest first for charting


# ---------------------------------------------------------------------------
# GET /api/mode  — current BOT_ENV and arm status
# ---------------------------------------------------------------------------

@app.get("/api/mode")
async def get_mode() -> dict[str, Any]:
    settings, config = load_config()
    arm_path = Path("data") / "live_arm.json"
    armed = False
    arm_expires = None
    arm_remaining = 0
    if arm_path.exists():
        try:
            import json as _json
            payload = _json.loads(arm_path.read_text(encoding="utf-8"))
            expires_at = datetime.fromisoformat(str(payload.get("expires_at")).replace("Z", "+00:00")).astimezone(timezone.utc)
            remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                armed = True
                arm_expires = expires_at.isoformat()
                arm_remaining = int(remaining)
        except Exception:
            pass
    return {
        "bot_env": settings.bot_env,
        "live_trading_enabled": settings.live_trading_enabled,
        "paper_only": config.bot.paper_only,
        "armed": armed,
        "arm_expires": arm_expires,
        "arm_remaining_seconds": arm_remaining,
    }


# ---------------------------------------------------------------------------
# POST /api/set-mode  — flip BOT_ENV in .env
# ---------------------------------------------------------------------------

class SetModeRequest(BaseModel):
    mode: str  # "paper" or "live"

@app.post("/api/set-mode")
async def set_mode_endpoint(req: SetModeRequest) -> dict[str, Any]:
    if req.mode not in {"paper", "live"}:
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")
    env_path = Path(".env")
    if not env_path.exists():
        raise HTTPException(status_code=404, detail=".env file not found in working directory")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    updated = False
    new_lines = []
    for line in lines:
        if line.startswith("BOT_ENV="):
            new_lines.append(f"BOT_ENV={req.mode}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"BOT_ENV={req.mode}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # Re-read to confirm
    settings, config = load_config()
    return {
        "ok": True,
        "bot_env": settings.bot_env,
        "live_trading_enabled": settings.live_trading_enabled,
        "paper_only": config.bot.paper_only,
    }


# ---------------------------------------------------------------------------
# POST /api/arm-live  — write the arm file (required before live order submits)
# ---------------------------------------------------------------------------

class ArmLiveRequest(BaseModel):
    minutes: int = 5

@app.post("/api/arm-live")
async def arm_live_endpoint(req: ArmLiveRequest) -> dict[str, Any]:
    settings, config = load_config()
    if settings.bot_env != "live" or not settings.live_trading_enabled or config.bot.paper_only:
        raise HTTPException(status_code=403, detail="Cannot arm: BOT_ENV must be 'live'. Use set-mode first.")
    if not (1 <= req.minutes <= 60):
        raise HTTPException(status_code=400, detail="minutes must be 1–60")

    import json as _json
    from datetime import timedelta
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=req.minutes)
    arm_path = Path("data") / "live_arm.json"
    arm_path.parent.mkdir(parents=True, exist_ok=True)
    arm_path.write_text(_json.dumps({
        "armed_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at.isoformat(),
    }, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "armed": True,
        "expires_at": expires_at.isoformat(),
        "arm_remaining_seconds": req.minutes * 60,
    }


# ---------------------------------------------------------------------------
# POST /api/disarm-live  — remove the arm file
# ---------------------------------------------------------------------------

@app.post("/api/disarm-live")
async def disarm_live_endpoint() -> dict[str, Any]:
    arm_path = Path("data") / "live_arm.json"
    if arm_path.exists():
        arm_path.unlink()
    return {"ok": True, "armed": False}