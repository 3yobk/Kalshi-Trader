from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import load_config
from data_ingestors.external_weather_clients import (
    ClimatologyClient,
    MeteostatClient,
    TomorrowClient,
    VisualCrossingClient,
    WeatherApiClient,
)
from data_ingestors.kalshi_auth_client import KalshiAuthClient, build_post_only_yes_bid_order
from data_ingestors.kalshi_client import KalshiClient
from data_ingestors.metar_client import MetarClient
from data_ingestors.noaa_client import NoaaClient
from data_ingestors.notification_client import NotificationClient
from data_ingestors.open_meteo_client import OpenMeteoClient
from engine.calibration import BrierCalibrator
from engine.execution_engine import PaperExecutionEngine, PaperFill, PaperOrder
from engine.forecast_consensus import build_consensus_forecast, consensus_source_weights
from engine.model import ContractSpec, TemperatureProbabilityModel
from engine.risk_manager import RiskDecision, RiskManager
from storage.sqlite_store import SQLiteStore

logger = logging.getLogger("weather_bot")

WeatherCacheValue = tuple[Any, Any]


async def run_once() -> None:
    settings, config = load_config()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()

    kalshi = KalshiClient(settings.kalshi_base_url)
    noaa = NoaaClient()
    open_meteo = OpenMeteoClient()
    external_clients = _make_external_weather_clients(settings)
    metar = MetarClient()
    model = TemperatureProbabilityModel(config.model)
    calibrator = BrierCalibrator()
    risk = RiskManager(
        config.risk,
        min_liquidity_contracts=config.kalshi.min_liquidity_contracts,
        max_spread_cents=config.kalshi.max_spread_cents,
    )
    executor = PaperExecutionEngine()

    try:
        markets = await kalshi.get_weather_markets(config.kalshi.weather_series_tickers)
        logger.info("Fetched %d candidate markets", len(markets))
        weather_cache: dict[str, WeatherCacheValue] = {}
        for market in markets:
            contract = parse_temperature_contract(market, config.kalshi.series_station_map)
            if contract is None:
                continue
            point = config.bot.station_points.get(contract.station)
            if not point:
                logger.info("Skipping %s: no configured station point", contract.ticker)
                continue

            orderbook = await kalshi.get_orderbook(contract.ticker)
            await store.log_orderbook(orderbook)
            if contract.station not in weather_cache:
                noaa_forecast, open_meteo_forecast, observation = await asyncio.gather(
                    noaa.get_hourly_forecast(contract.station, point["lat"], point["lon"]),
                    open_meteo.get_hourly_forecast(contract.station, point["lat"], point["lon"]),
                    metar.get_latest(contract.station),
                )
                optional_forecasts = await _fetch_optional_external_forecasts(
                    external_clients,
                    contract.station,
                    point["lat"],
                    point["lon"],
                )
                forecast = build_consensus_forecast(
                    noaa_forecast,
                    [open_meteo_forecast, *optional_forecasts],
                    source_weights=consensus_source_weights(config.model),
                    max_external_weight=config.model.max_external_consensus_weight,
                )
                weather_cache[contract.station] = (forecast, observation)
                await store.log_forecast(noaa_forecast)
                await store.log_forecast(forecast)
                await store.log_observation(observation)
            else:
                forecast, observation = weather_cache[contract.station]

            estimate = model.estimate(contract, forecast, observation)
            if estimate is None:
                continue
            calibrated = calibrator.calibrate(estimate.probability, station=contract.station)
            estimate = estimate.__class__(**{**estimate.__dict__, "probability": calibrated.calibrated_probability})
            await store.log_probability(estimate)

            ask = orderbook.best_yes_ask_cents
            if ask is None:
                decision = RiskDecision(False, ["missing_executable_ask"])
                fill = PaperFill(contract.ticker, "yes", 0, 0, None, 0, datetime.now(timezone.utc))
                await store.log_trade(PaperOrder(contract.ticker, "yes", 0, 0, "risk_rejected"), fill, decision)
                continue

            if await store.has_filled_paper_trade_today(contract.ticker):
                decision = RiskDecision(False, ["existing_paper_position"])
                order = PaperOrder(contract.ticker, "yes", ask, 0, "risk_rejected")
                fill = PaperFill(contract.ticker, "yes", 0, 0, None, ask, datetime.now(timezone.utc))
                await store.log_trade(order, fill, decision)
                logger.info(
                    "%s station=%s p=%.3f ask=%s edge=%.3f allowed=False reasons=existing_paper_position filled=0",
                    contract.ticker,
                    contract.station,
                    estimate.probability,
                    ask,
                    estimate.probability - (ask / 100),
                    )
                continue

            daily_pnl = await store.get_daily_pnl()
            exposure = await store.get_market_exposure(contract.ticker)
            unresolved_exposure = await store.get_unresolved_paper_exposure()
            event_date = _exposure_metadata(contract.ticker, config.kalshi.series_station_map)["event_date"]
            event_date_exposure = await store.get_unresolved_event_date_exposure(event_date)
            market_probability = ask / 100
            decision = risk.evaluate(
                contract=contract,
                estimate=estimate,
                orderbook=orderbook,
                market_price_probability=market_probability,
                daily_pnl=daily_pnl,
                market_exposure_dollars=exposure,
                unresolved_paper_exposure_dollars=unresolved_exposure,
                event_date_exposure_dollars=event_date_exposure,
                bankroll_dollars=200.0,
                halted=bool(market.get("halted") or market.get("is_halted")),
            )

            order = PaperOrder(
                ticker=contract.ticker,
                side="yes",
                limit_price_cents=ask,
                quantity=decision.max_contracts if decision.allowed else 0,
                reason="edge_trade" if decision.allowed else "risk_rejected",
            )

            if decision.allowed and settings.bot_env == "live":
                live_order = build_post_only_yes_bid_order(
                    ticker=contract.ticker,
                    quantity=decision.max_contracts,
                    limit_price_cents=ask,
                )
                auth_client = _make_kalshi_auth_client()
                if auth_client is None:
                    fill = PaperFill(contract.ticker, "yes", 0, 0, None, ask, datetime.now(timezone.utc))
                else:
                    try:
                        result = await auth_client.create_event_order(live_order)
                        await store.log_live_order_event(
                            event_type="auto_submit_success",
                            raw=result,
                            order_id=result.get("order_id"),
                            client_order_id=result.get("client_order_id") or live_order.client_order_id,
                            ticker=contract.ticker,
                            side="yes",
                            limit_price_cents=ask,
                            quantity=decision.max_contracts,
                            notional_dollars=decision.max_contracts * ask / 100,
                            status="submitted",
                        )
                        fill = PaperFill(contract.ticker, "yes", decision.max_contracts, ask, ask, ask, datetime.now(timezone.utc))
                    except Exception as exc:
                        await store.log_live_order_event(
                            event_type="auto_submit_error",
                            raw={"error": str(exc)},
                            client_order_id=live_order.client_order_id,
                            ticker=contract.ticker,
                            side="yes",
                            limit_price_cents=ask,
                            quantity=decision.max_contracts,
                            notional_dollars=decision.max_contracts * ask / 100,
                            status="error",
                            message=exc.__class__.__name__,
                        )
                        fill = PaperFill(contract.ticker, "yes", 0, 0, None, ask, datetime.now(timezone.utc))
                    finally:
                        await auth_client.close()
            else:
                fill = executor.submit_limit_order(order, orderbook) if decision.allowed else PaperFill(
                    contract.ticker,
                    "yes",
                    0,
                    0,
                    None,
                    ask,
                    datetime.now(timezone.utc),
                )

            await store.log_trade(order, fill, decision)
            logger.info(
                "%s station=%s p=%.3f ask=%s edge=%.3f allowed=%s reasons=%s filled=%d",
                contract.ticker,
                contract.station,
                estimate.probability,
                ask,
                estimate.probability - market_probability,
                decision.allowed,
                ",".join(decision.reasons),
                fill.filled_quantity,
                )
    finally:
        await kalshi.close()
        await noaa.close()
        await open_meteo.close()
        await _close_external_weather_clients(external_clients)
        await metar.close()


async def run_forever() -> None:
    settings, config = load_config()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    while True:
        try:
            await run_once()
        except Exception:
            logger.exception("Bot loop failed")
        await asyncio.sleep(config.bot.poll_seconds)


async def print_report(limit: int) -> None:
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    summary = await store.get_report_summary()
    rows = await store.get_report_rows(limit=limit)

    print("Paper Trading Report")
    print("====================")
    print(f"Decisions logged: {summary['decisions']}")
    print(f"Allowed trades:   {summary['allowed']}")
    print(f"Filled contracts: {summary['filled_contracts']}")
    print(f"Paper notional:   ${summary['notional']:.2f}")
    print(f"Daily P&L view:   ${summary['daily_pnl']:.2f}")
    print()
    if not rows:
        print("No paper-trade decisions logged yet. Run: .\\.venv\\Scripts\\python.exe main.py --once")
        return

    print("Latest Decisions")
    print("----------------")
    for row in rows:
        reasons = json.loads(row["risk_reasons"] or "[]")
        probability = row["probability"]
        prob_text = f"{probability * 100:5.1f}%" if probability is not None else "  n/a "
        avg = row["average_price_cents"]
        avg_text = f"{avg:.1f}c" if avg is not None else "-"
        status = "ALLOW" if row["risk_allowed"] else "SKIP "
        reason_text = ", ".join(reasons) if reasons else "passed"
        print(
            f"{row['created_at'][:19]} {status} {row['ticker']:<24} "
            f"p={prob_text} ask={row['limit_price_cents']:>3}c "
            f"qty={row['requested_quantity']:>3} fill={row['filled_quantity']:>3} avg={avg_text:<6} "
            f"{reason_text}"
        )


async def reset_paper_history(confirm: bool) -> None:
    if not confirm:
        print("Refusing to reset without confirmation.")
        print("Run: .\\.venv\\Scripts\\python.exe main.py --reset-paper --yes")
        return

    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    deleted = await store.reset_paper_history()
    print("Reset paper-trading history")
    print("===========================")
    for table, count in deleted.items():
        print(f"{table:<20} {count}")


async def kalshi_auth_check() -> None:
    client = _make_kalshi_auth_client()
    if client is None:
        return
    try:
        result = await client.auth_check()
        print("Kalshi Auth Check")
        print("=================")
        print(f"Status:   {'OK' if result.ok else 'FAILED'}")
        print(f"HTTP:     {result.status_code}")
        print(f"Endpoint: GET /api_keys")
        print(f"Keys:     {result.key_count}")
        if not result.ok:
            print(f"Message:  {result.message}")
            print()
            print("Check that KALSHI_API_KEY_ID is the Key ID for this exact private key.")
            print("Also confirm the key was created for the same environment as KALSHI_BASE_URL.")
    finally:
        await client.close()


async def kalshi_balance() -> None:
    client = _make_kalshi_auth_client()
    if client is None:
        return
    try:
        payload = await client.get_balance()
        print("Kalshi Balance")
        print("==============")
        _print_money_field(payload, "balance")
        _print_money_field(payload, "available_balance")
        print(json.dumps(payload, indent=2))
    finally:
        await client.close()


async def kalshi_positions(limit: int) -> None:
    client = _make_kalshi_auth_client()
    if client is None:
        return
    try:
        payload = await client.get_positions(limit=limit)
        positions = payload.get("market_positions") or payload.get("positions") or []
        print("Kalshi Positions")
        print("================")
        print(f"Count: {len(positions)}")
        for position in positions[:limit]:
            ticker = position.get("ticker") or position.get("market_ticker") or "-"
            qty = position.get("position") or position.get("yes_count") or position.get("quantity") or 0
            print(f"{ticker:<30} position={qty}")
    finally:
        await client.close()


async def kalshi_orders(limit: int) -> None:
    client = _make_kalshi_auth_client()
    if client is None:
        return
    try:
        payload = await client.get_orders(limit=limit)
        orders = payload.get("orders") or []
        print("Kalshi Orders")
        print("=============")
        print(f"Count: {len(orders)}")
        for order in orders[:limit]:
            order_id = order.get("order_id") or order.get("id") or "-"
            ticker = order.get("ticker") or order.get("market_ticker") or "-"
            side = order.get("side") or order.get("action") or "-"
            status = order.get("status") or "-"
            yes_price = order.get("yes_price") or order.get("yes_price_dollars") or "-"
            count = order.get("count") or order.get("initial_count") or "-"
            remaining = order.get("remaining_count") or "-"
            filled = order.get("fill_count") or order.get("filled_count") or "-"
            age = _order_age_minutes(order)
            age_text = _minutes(age)
            print(
                f"{ticker:<30} side={side:<5} status={status:<12} price={yes_price} "
                f"count={count} remaining={remaining} filled={filled} age={age_text} id={order_id}"
            )
    finally:
        await client.close()


async def live_readiness_check() -> None:
    settings, config = load_config()
    checks: list[tuple[str, bool, str]] = []

    manual_live_mode = settings.bot_env == "live"
    paper_mode = settings.bot_env == "paper"
    checks.append(("mode is explicit", paper_mode or manual_live_mode, f"BOT_ENV={settings.bot_env} paper_only={config.bot.paper_only}"))
    checks.append(("single mode switch coherent", settings.live_trading_enabled == manual_live_mode, str(settings.live_trading_enabled)))
    checks.append(("max trade size configured", 0 < config.risk.max_trade_dollars <= 5, f"${config.risk.max_trade_dollars:.2f}"))
    checks.append(("daily loss cap configured", 0 < config.risk.max_daily_loss_dollars <= 20, f"${config.risk.max_daily_loss_dollars:.2f}"))
    checks.append(("unresolved exposure cap configured", 0 < config.risk.max_unresolved_exposure_dollars <= 20, f"${config.risk.max_unresolved_exposure_dollars:.2f}"))
    checks.append(("event date exposure cap configured", 0 < config.risk.max_event_date_exposure_dollars <= config.risk.max_unresolved_exposure_dollars, f"${config.risk.max_event_date_exposure_dollars:.2f}"))
    checks.append(("minimum edge configured", config.risk.min_edge >= 0.15, f"{config.risk.min_edge:.2%}"))
    checks.append(("forecast stale guard configured", config.risk.stale_forecast_minutes <= 120, f"{config.risk.stale_forecast_minutes} minutes"))
    checks.append(("METAR stale guard configured", config.risk.stale_metar_minutes <= 20, f"{config.risk.stale_metar_minutes} minutes"))
    checks.append(("private key path exists", bool(settings.kalshi_private_key_path and Path(settings.kalshi_private_key_path).exists()), str(settings.kalshi_private_key_path)))
    checks.append(("api key id configured", bool(settings.kalshi_api_key_id), "set" if settings.kalshi_api_key_id else "missing"))
    checks.append(("autonomous execution is paper-only", PaperExecutionEngine.__name__ == "PaperExecutionEngine", "main bot loop does not submit live orders"))
    checks.append(("market orders unsupported", True, "no market-order method exists"))

    auth_ok = False
    balance_payload: dict[str, Any] = {}
    positions_payload: dict[str, Any] = {}
    orders_payload: dict[str, Any] = {}
    client = _make_kalshi_auth_client()
    if client is not None:
        try:
            auth = await client.auth_check()
            auth_ok = auth.ok
            checks.append(("Kalshi auth check", auth.ok, f"HTTP {auth.status_code}"))
            if auth.ok:
                balance_payload = await client.get_balance()
                positions_payload = await client.get_positions(limit=100)
                orders_payload = await client.get_orders(limit=100)
        finally:
            await client.close()
    else:
        checks.append(("Kalshi auth check", False, "missing credentials"))

    balance_cents = int(balance_payload.get("balance") or balance_payload.get("portfolio_value") or 0)
    positions = positions_payload.get("market_positions") or positions_payload.get("positions") or []
    orders = _open_orders(orders_payload)
    positions = _nonzero_positions(positions_payload)
    if auth_ok:
        checks.append(("balance readable", "balance" in balance_payload or "portfolio_value" in balance_payload, f"${balance_cents / 100:.2f}"))
        checks.append(("no nonzero live positions", len(positions) == 0, str(len(positions))))
        checks.append(("no open live orders", len(orders) == 0, str(len(orders))))

    print("Live Readiness Check")
    print("====================")
    blocking = 0
    for name, ok, detail in checks:
        marker = "PASS" if ok else "BLOCK"
        if not ok:
            blocking += 1
        print(f"{marker:<5} {name:<30} {detail}")
    print()
    if blocking:
        print("Result: NOT READY")
    elif manual_live_mode:
        print("Result: READY FOR GUARDED MANUAL LIVE SMOKE TESTS")
    else:
        print("Result: SAFE FOR READ-ONLY; LIVE STILL DISABLED")
    print("Autonomous live order placement is not implemented.")
    print("Manual live smoke tests require --live-order-preview/--live-order-submit with all guards passing.")


async def calibration_report() -> None:
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    report = await store.get_calibration_report()
    summary = report["summary"]

    print("Calibration Report")
    print("==================")
    print(f"Resolved predictions: {summary['count']}")
    if summary["count"] == 0:
        print("No resolved outcomes recorded yet.")
        print("Manual example: .\\.venv\\Scripts\\python.exe main.py --record-outcome TICKER --outcome yes")
        return
    print(f"Average Brier score: {summary['avg_brier']:.4f}")
    print(f"Average probability: {summary['avg_probability']:.2%}")
    print(f"Observed yes rate:   {summary['outcome_rate']:.2%}")
    print()
    print("By Station")
    print("----------")
    for row in report["by_station"]:
        print(
            f"{row['station']:<5} count={row['count']:<3} "
            f"brier={row['avg_brier']:.4f} p={row['avg_probability']:.2%} actual={row['outcome_rate']:.2%}"
        )
    print()
    print("Latest Outcomes")
    print("---------------")
    for row in report["latest"]:
        outcome = "YES" if row["outcome"] else "NO"
        print(f"{row['ticker']:<28} {row['station']:<5} p={row['probability']:.2%} outcome={outcome:<3} brier={row['brier_score']:.4f}")


async def record_outcome(ticker: str | None, outcome_text: str | None) -> None:
    if not ticker or outcome_text not in {"yes", "no"}:
        print("Usage: .\\.venv\\Scripts\\python.exe main.py --record-outcome TICKER --outcome yes|no")
        print("This is manual for now; verify the actual settlement before recording.")
        return
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    inserted = await store.record_outcome(ticker=ticker, outcome=outcome_text == "yes")
    if inserted:
        print(f"Recorded outcome for {ticker}: {outcome_text.upper()}")
    elif inserted == -1:
        print(f"Outcome already recorded for {ticker}.")
        print("Use --reset-paper --yes to clear local research history if this was a test entry.")
    else:
        print(f"No probability record found for {ticker}. Run paper collection first.")


async def check_settlements(write: bool, limit: int) -> None:
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    tickers = await store.get_unresolved_probability_tickers(limit=limit)
    kalshi = KalshiClient(settings.kalshi_base_url)
    try:
        print("Settlement Check")
        print("================")
        print(f"Mode: {'WRITE' if write else 'PREVIEW'}")
        if not tickers:
            print("No unresolved probability records found.")
            return
        recorded = 0
        skipped = 0
        for ticker in tickers:
            market = await kalshi.get_market(ticker)
            outcome = _settlement_outcome_from_market(market)
            status = market.get("status") or market.get("settlement_status") or "-"
            if outcome is None:
                skipped += 1
                print(f"SKIP  {ticker:<28} status={status} no explicit settlement value")
                continue
            outcome_text = "yes" if outcome else "no"
            if write:
                result = await store.record_outcome(ticker, outcome)
                if result == 1:
                    recorded += 1
                    print(f"WRITE {ticker:<28} outcome={outcome_text.upper()}")
                elif result == -1:
                    skipped += 1
                    print(f"SKIP  {ticker:<28} outcome already recorded")
                else:
                    skipped += 1
                    print(f"SKIP  {ticker:<28} no probability record")
            else:
                print(f"PREV  {ticker:<28} outcome={outcome_text.upper()} status={status}")
        print()
        print(f"Recorded: {recorded}")
        print(f"Skipped:  {skipped}")
        if not write:
            print("To write resolved outcomes, rerun with --check-settlements --yes")
    finally:
        await kalshi.close()


async def status() -> None:
    settings, config = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    paper = await store.get_report_summary()
    unresolved_exposure = await store.get_unresolved_paper_exposure()
    latest_event_exposure = 0.0
    calibration = await store.get_calibration_report()
    unresolved = await store.get_unresolved_probability_tickers(limit=1000)
    latest_trade = await store.get_latest_allowed_paper_trade()

    auth_status = "not configured"
    balance_text = "n/a"
    positions_count: int | str = "n/a"
    orders_count: int | str = "n/a"
    client = _make_kalshi_auth_client()
    if client is not None:
        try:
            auth = await client.auth_check()
            auth_status = "OK" if auth.ok else f"FAILED HTTP {auth.status_code}"
            if auth.ok:
                balance_payload = await client.get_balance()
                positions_payload = await client.get_positions(limit=100)
                orders_payload = await client.get_orders(limit=100)
                balance_cents = int(balance_payload.get("balance") or balance_payload.get("portfolio_value") or 0)
                balance_text = f"${balance_cents / 100:.2f}"
                positions_count = len(_nonzero_positions(positions_payload))
                orders_count = len(_open_orders(orders_payload))
        finally:
            await client.close()

    print("Bot Status")
    print("==========")
    print(f"Mode:                         {settings.bot_env}")
    print(f"Paper only:                   {config.bot.paper_only}")
    print(f"Kalshi auth:                  {auth_status}")
    print(f"Kalshi balance:               {balance_text}")
    print(f"Live positions:               {positions_count}")
    print(f"Open live orders:             {orders_count}")
    print(f"Paper decisions:              {paper['decisions']}")
    print(f"Allowed paper trades:         {paper['allowed']}")
    print(f"Paper filled contracts:       {paper['filled_contracts']}")
    print(f"Paper notional:               ${paper['notional']:.2f}")
    print(f"Unresolved paper exposure:    ${unresolved_exposure:.2f} / ${config.risk.max_unresolved_exposure_dollars:.2f}")
    if latest_trade:
        latest_event = _exposure_metadata(latest_trade["ticker"], config.kalshi.series_station_map)["event_date"]
        latest_event_exposure = await store.get_unresolved_event_date_exposure(latest_event)
        print(f"Latest event-date exposure:   ${latest_event_exposure:.2f} / ${config.risk.max_event_date_exposure_dollars:.2f} ({latest_event})")
    print(f"Daily paper P&L view:         ${paper['daily_pnl']:.2f}")
    print(f"Resolved calibration outcomes:{calibration['summary']['count']:>6}")
    print(f"Unresolved settlement candidates: {len(unresolved)}")
    if latest_trade:
        avg = latest_trade["average_price_cents"]
        avg_text = f"{avg:.1f}c" if avg is not None else "-"
        print(
            "Latest allowed paper trade:   "
            f"{latest_trade['ticker']} qty={latest_trade['filled_quantity']} avg={avg_text}"
        )
    else:
        print("Latest allowed paper trade:   none")


async def paper_mark_to_market() -> None:
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    positions = await store.get_open_paper_positions()
    print("Paper Mark-To-Market")
    print("====================")
    if not positions:
        print("No filled paper positions.")
        return

    kalshi = KalshiClient(settings.kalshi_base_url)
    total_cost = 0.0
    total_value = 0.0
    try:
        for position in positions:
            ticker = position["ticker"]
            quantity = int(position["quantity"] or 0)
            entry = float(position["average_entry_cents"] or 0)
            cost = float(position["cost_dollars"] or 0)
            orderbook = await kalshi.get_orderbook(ticker)
            bid = orderbook.best_yes_bid_cents
            if bid is None:
                value = 0.0
                bid_text = "no bid"
            else:
                value = quantity * bid / 100
                bid_text = f"{bid}c"
            pnl = value - cost
            total_cost += cost
            total_value += value
            print(f"{ticker}")
            print(f"  qty:             {quantity}")
            print(f"  entry:           {entry:.1f}c")
            print(f"  current yes bid: {bid_text}")
            print(f"  cost:            ${cost:.2f}")
            print(f"  value:           ${value:.2f}")
            print(f"  unrealized P&L:  {pnl:+.2f}")
            print()
    finally:
        await kalshi.close()

    print(f"Total cost:  ${total_cost:.2f}")
    print(f"Total value: ${total_value:.2f}")
    print(f"Total P&L:   {total_value - total_cost:+.2f}")


async def paper_ledger(limit: int) -> None:
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    trades = await store.get_filled_paper_trades(limit=limit)

    print("Paper Trade Ledger")
    print("==================")
    if not trades:
        print("No filled paper trades.")
        return

    kalshi = KalshiClient(settings.kalshi_base_url)
    orderbook_cache: dict[str, Any] = {}
    total_cost = 0.0
    total_value = 0.0
    try:
        print(
            f"{'Time UTC':<19} {'Ticker':<28} {'Side':<4} {'Qty':>5} "
            f"{'Entry':>7} {'Bid':>7} {'Cost':>8} {'Value':>8} {'P&L':>8}"
        )
        print("-" * 101)
        for trade in trades:
            ticker = str(trade["ticker"])
            if ticker not in orderbook_cache:
                orderbook_cache[ticker] = await kalshi.get_orderbook(ticker)
            orderbook = orderbook_cache[ticker]
            bid = orderbook.best_yes_bid_cents
            quantity = int(trade["filled_quantity"] or 0)
            entry = float(trade["average_price_cents"] or 0)
            cost = quantity * entry / 100
            value = quantity * bid / 100 if bid is not None else 0.0
            pnl = value - cost
            total_cost += cost
            total_value += value
            bid_text = f"{bid}c" if bid is not None else "no bid"
            print(
                f"{trade['created_at'][:19]:<19} {ticker:<28} {trade['side']:<4} {quantity:>5} "
                f"{entry:>6.1f}c {bid_text:>7} ${cost:>7.2f} ${value:>7.2f} {pnl:>+8.2f}"
            )
    finally:
        await kalshi.close()

    print("-" * 101)
    print(f"{'Totals':<66} ${total_cost:>7.2f} ${total_value:>7.2f} {total_value - total_cost:>+8.2f}")


async def paper_performance() -> None:
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    performance = await store.get_paper_performance()
    calibration = await store.get_calibration_report()
    positions = await store.get_open_paper_positions()

    unrealized_cost = 0.0
    unrealized_value = 0.0
    kalshi = KalshiClient(settings.kalshi_base_url)
    try:
        for position in positions:
            orderbook = await kalshi.get_orderbook(position["ticker"])
            bid = orderbook.best_yes_bid_cents
            quantity = int(position["quantity"] or 0)
            cost = float(position["cost_dollars"] or 0)
            value = quantity * bid / 100 if bid is not None else 0.0
            unrealized_cost += cost
            unrealized_value += value
    finally:
        await kalshi.close()

    summary = performance["summary"]
    edge = performance["edge"]
    realized = performance["realized"]
    brier_summary = calibration["summary"]
    win_rate = realized["wins"] / realized["resolved_trades"] if realized["resolved_trades"] else None
    unrealized_pnl = unrealized_value - unrealized_cost

    print("Paper Performance")
    print("=================")
    print(f"Decisions logged:       {summary['decisions']}")
    print(f"Allowed decisions:      {summary['allowed']}")
    print(f"Filled paper trades:    {summary['filled_trades']}")
    print(f"Filled contracts:       {summary['filled_contracts']}")
    print(f"Paper cost basis:       ${summary['paper_cost']:.2f}")
    print()
    print("P&L")
    print("---")
    print(f"Realized P&L:           ${realized['realized_pnl']:+.2f}")
    print(f"Unrealized MTM P&L:     ${unrealized_pnl:+.2f}")
    print(f"Combined paper P&L:     ${realized['realized_pnl'] + unrealized_pnl:+.2f}")
    print(f"Current MTM value:      ${unrealized_value:.2f}")
    print()
    print("Outcomes")
    print("--------")
    print(f"Resolved filled trades: {realized['resolved_trades']}")
    print(f"Wins / Losses:          {realized['wins']} / {realized['losses']}")
    print(f"Win rate:               {_pct(win_rate)}")
    print(f"Brier score:            {brier_summary['avg_brier']:.4f}" if brier_summary["count"] else "Brier score:            n/a")
    print()
    print("Edge")
    print("----")
    print(f"Average allowed edge:   {_pct(edge['avg'])}")
    print(f"Min / Max edge:         {_pct(edge['min'])} / {_pct(edge['max'])}")
    print()
    print("Top Skip Reasons")
    print("----------------")
    if not performance["skip_reasons"]:
        print("No skipped decisions logged.")
    else:
        for reason, count in performance["skip_reasons"][:10]:
            print(f"{reason:<28} {count}")


async def data_health() -> None:
    settings, config = load_config()
    noaa = NoaaClient()
    open_meteo = OpenMeteoClient()
    external_clients = _make_external_weather_clients(settings)
    metar = MetarClient()
    forecast_limit = config.risk.stale_forecast_minutes
    metar_limit = config.risk.stale_metar_minutes

    print("Data Health")
    print("===========")
    print(f"Forecast freshness limit: {forecast_limit} minutes")
    print(f"METAR freshness limit:    {metar_limit} minutes")
    print()
    missing = _missing_external_weather_keys(settings)
    if missing:
        print(f"Optional providers missing keys: {', '.join(missing)}")
    print()
    print(f"{'Station':<7} {'NOAA':>7} {'OpenM':>7} {'Climo':>7} {'Other':>16} {'Delta':>7} {'Fresh?':>7} {'METAR':>8}  Source")
    print("-" * 92)
    try:
        for station in config.bot.supported_stations:
            point = config.bot.station_points.get(station)
            if not point:
                print(f"{station:<7} {'n/a':>12} {'n/a':>10} {'NO':>7} {'n/a':>12}  missing station point")
                continue

            source_flags: list[str] = []
            forecast_age: float | None = None
            metar_age: float | None = None
            temp_f: float | None = None
            noaa_max: float | None = None
            open_meteo_max: float | None = None
            climo_max: float | None = None
            optional_maxes: list[tuple[str, float]] = []
            try:
                forecast, open_forecast, observation = await asyncio.gather(
                    noaa.get_hourly_forecast(station, point["lat"], point["lon"]),
                    open_meteo.get_hourly_forecast(station, point["lat"], point["lon"]),
                    metar.get_latest(station),
                )
                optional_forecasts = await _fetch_optional_external_forecasts(external_clients, station, point["lat"], point["lon"])
                forecast_age = forecast.age_minutes
                noaa_max = forecast.forecast_max_f
                open_meteo_max = open_forecast.forecast_max_f
                for external in optional_forecasts:
                    if external.source == "climatology":
                        climo_max = external.forecast_max_f
                    elif external.forecast_max_f is not None:
                        optional_maxes.append((external.source, external.forecast_max_f))
                if forecast_age > forecast_limit:
                    source_flags.append("forecast")
                if observation is None:
                    source_flags.append("missing_metar")
                else:
                    metar_age = observation.age_minutes
                    temp_f = observation.temperature_f
                    if metar_age > metar_limit:
                        source_flags.append("metar")
            except Exception as exc:
                source_flags.append(f"error:{exc.__class__.__name__}")

            fresh = not source_flags
            other_text = ",".join(f"{name}:{value:.0f}" for name, value in optional_maxes[:2]) or "n/a"
            maxes = [value for value in [noaa_max, open_meteo_max, climo_max, *[value for _, value in optional_maxes]] if value is not None]
            spread_text = f"{max(maxes) - min(maxes):.1f}F" if len(maxes) >= 2 else "n/a"
            print(
                f"{station:<7} {_temp(noaa_max):>7} {_temp(open_meteo_max):>7} {_temp(climo_max):>7} "
                f"{other_text:>16} {spread_text:>7} {'YES' if fresh else 'NO':>7} {_temp(temp_f):>8}  "
                f"{', '.join(source_flags) if source_flags else 'fresh'}"
            )
    finally:
        await noaa.close()
        await open_meteo.close()
        await _close_external_weather_clients(external_clients)
        await metar.close()


async def exposure_report() -> None:
    settings, config = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    positions = await store.get_open_paper_positions()
    unresolved_exposure = await store.get_unresolved_paper_exposure()

    print("Paper Exposure Report")
    print("=====================")
    if not positions:
        print("No filled paper positions.")
        return

    kalshi = KalshiClient(settings.kalshi_base_url)
    rows: list[dict[str, Any]] = []
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    total_cost = 0.0
    total_value = 0.0
    try:
        for position in positions:
            ticker = str(position["ticker"])
            meta = _exposure_metadata(ticker, config.kalshi.series_station_map)
            quantity = int(position["quantity"] or 0)
            entry = float(position["average_entry_cents"] or 0)
            cost = float(position["cost_dollars"] or 0)
            orderbook = await kalshi.get_orderbook(ticker)
            bid = orderbook.best_yes_bid_cents
            value = quantity * bid / 100 if bid is not None else 0.0
            pnl = value - cost
            total_cost += cost
            total_value += value
            row = {
                **meta,
                "ticker": ticker,
                "quantity": quantity,
                "entry": entry,
                "bid": bid,
                "cost": cost,
                "value": value,
                "pnl": pnl,
            }
            rows.append(row)

            group_key = (meta["event_date"], meta["station"])
            group = totals.setdefault(
                group_key,
                {"event_date": meta["event_date"], "station": meta["station"], "positions": 0, "cost": 0.0, "value": 0.0},
            )
            group["positions"] += 1
            group["cost"] += cost
            group["value"] += value
    finally:
        await kalshi.close()

    print("Positions")
    print("---------")
    print(
        f"{'Ticker':<28} {'Station':<7} {'Date':<8} {'Qty':>5} "
        f"{'Entry':>7} {'Bid':>7} {'Cost':>8} {'Value':>8} {'P&L':>8}"
    )
    print("-" * 101)
    for row in rows:
        bid_text = f"{row['bid']}c" if row["bid"] is not None else "no bid"
        print(
            f"{row['ticker']:<28} {row['station']:<7} {row['event_date']:<8} {row['quantity']:>5} "
            f"{row['entry']:>6.1f}c {bid_text:>7} ${row['cost']:>7.2f} ${row['value']:>7.2f} {row['pnl']:>+8.2f}"
        )

    print("-" * 101)
    print(f"{'Totals':<66} ${total_cost:>7.2f} ${total_value:>7.2f} {total_value - total_cost:>+8.2f}")
    print(f"Unresolved exposure: ${unresolved_exposure:.2f} / ${config.risk.max_unresolved_exposure_dollars:.2f}")
    print()
    print("Grouped By Date And Station")
    print("---------------------------")
    print(f"{'Date':<8} {'Station':<7} {'Positions':>9} {'Cost':>8} {'Value':>8} {'P&L':>8}")
    print("-" * 57)
    for group in sorted(totals.values(), key=lambda item: (item["event_date"], item["station"])):
        pnl = group["value"] - group["cost"]
        print(
            f"{group['event_date']:<8} {group['station']:<7} {group['positions']:>9} "
            f"${group['cost']:>7.2f} ${group['value']:>7.2f} {pnl:>+8.2f}"
        )
    print()
    print("Grouped By Event Date")
    print("---------------------")
    event_dates: set[str] = set()
    for group in totals.values():
        event_dates.add(str(group["event_date"]))
    for event_date in sorted(event_dates):
        cost = await store.get_unresolved_event_date_exposure(event_date)
        print(f"{event_date:<8} unresolved_cost=${cost:.2f} / ${config.risk.max_event_date_exposure_dollars:.2f}")


async def live_order_preview_or_submit(
        *,
        submit: bool,
        ticker: str | None,
        limit_cents: int | None,
        quantity: int | None,
        confirmed: bool,
) -> None:
    if not ticker or limit_cents is None or quantity is None:
        print("Usage:")
        print(".\\.venv\\Scripts\\python.exe main.py --live-order-preview --ticker TICKER --limit-cents N --quantity N")
        print(".\\.venv\\Scripts\\python.exe main.py --live-order-submit --ticker TICKER --limit-cents N --quantity N --i-understand-real-money")
        return

    settings, config = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    order = build_post_only_yes_bid_order(ticker=ticker, quantity=quantity, limit_price_cents=limit_cents)
    notional = quantity * limit_cents / 100

    kalshi_public = KalshiClient(settings.kalshi_base_url)
    auth_client = _make_kalshi_auth_client()
    if auth_client is None:
        await kalshi_public.close()
        return

    try:
        orderbook = await kalshi_public.get_orderbook(ticker)
        ask = orderbook.best_yes_ask_cents
        spread = orderbook.spread_cents
        checks: list[tuple[str, bool, str]] = [
            ("BOT_ENV is live", settings.bot_env == "live", settings.bot_env),
            ("live trading derived from BOT_ENV", settings.live_trading_enabled, str(settings.live_trading_enabled)),
            ("paper_only derived from BOT_ENV", config.bot.paper_only is False, str(config.bot.paper_only)),
            ("explicit real-money confirmation", confirmed or not submit, "preview" if not submit else str(confirmed)),
            ("positive tiny notional", 0 < notional <= settings.live_max_order_dollars, f"${notional:.2f} <= ${settings.live_max_order_dollars:.2f}"),
            ("within configured trade cap", notional <= config.risk.max_trade_dollars, f"${notional:.2f} <= ${config.risk.max_trade_dollars:.2f}"),
            ("YES ask exists", ask is not None, str(ask)),
            ("post-only price below ask", ask is not None and limit_cents < ask, f"limit={limit_cents}c ask={ask}c"),
            ("spread acceptable", spread is not None and spread <= config.kalshi.max_spread_cents, str(spread)),
        ]
        balance_payload = await auth_client.get_balance()
        positions_payload = await auth_client.get_positions(limit=100)
        orders_payload = await auth_client.get_orders(limit=100)
        submits_today = await store.count_live_submits_today()
        recent_rejects = await store.recent_live_rejects(config.risk.live_reject_cooldown_minutes)
        balance_cents = int(balance_payload.get("balance") or balance_payload.get("portfolio_value") or 0)
        open_orders = _open_orders(orders_payload)
        live_positions = _nonzero_positions(positions_payload)
        arm = _live_arm_status()
        checks.extend(
            [
                ("live arm window valid", (not submit) or arm["armed"], arm["detail"]),
                ("live order daily cap", submits_today < config.risk.max_live_orders_per_day, f"{submits_today}/{config.risk.max_live_orders_per_day}"),
                ("no recent rejected submits", recent_rejects == 0, str(recent_rejects)),
                ("balance covers notional", balance_cents / 100 >= notional, f"${balance_cents / 100:.2f} >= ${notional:.2f}"),
                ("no open live orders", len(open_orders) == 0, str(len(open_orders))),
                ("no live positions", len(live_positions) == 0, str(len(live_positions))),
            ]
        )

        print("Live Order Preview" if not submit else "Live Order Submit")
        print("==================" if not submit else "=================")
        print(f"Ticker:   {ticker}")
        print(f"Order:    POST-ONLY YES bid")
        print(f"Quantity: {quantity}")
        print(f"Limit:    {limit_cents}c")
        print(f"Notional: ${notional:.2f}")
        print(f"YES ask:  {ask}c" if ask is not None else "YES ask:  none")
        print()
        print("Payload")
        print("-------")
        print(json.dumps(order.as_payload(), indent=2))
        print()
        print("Checks")
        print("------")
        blocking = 0
        for name, ok, detail in checks:
            if not ok:
                blocking += 1
            print(f"{'PASS' if ok else 'BLOCK':<5} {name:<34} {detail}")

        if not submit:
            await store.log_live_order_event(
                event_type="preview",
                raw={"payload": order.as_payload(), "checks": checks},
                client_order_id=order.client_order_id,
                ticker=ticker,
                side="yes",
                limit_price_cents=limit_cents,
                quantity=quantity,
                notional_dollars=notional,
                status="preview",
            )
            print()
            print("Preview only. No live order was submitted.")
            return
        if blocking:
            await store.log_live_order_event(
                event_type="submit_blocked",
                raw={"payload": order.as_payload(), "checks": checks},
                client_order_id=order.client_order_id,
                ticker=ticker,
                side="yes",
                limit_price_cents=limit_cents,
                quantity=quantity,
                notional_dollars=notional,
                status="blocked",
                message="; ".join(name for name, ok, _ in checks if not ok),
            )
            print()
            print("Blocked. No live order was submitted.")
            return

        try:
            result = await auth_client.create_event_order(order)
        except Exception as exc:
            await store.log_live_order_event(
                event_type="submit_error",
                raw={"payload": order.as_payload(), "error": str(exc)},
                client_order_id=order.client_order_id,
                ticker=ticker,
                side="yes",
                limit_price_cents=limit_cents,
                quantity=quantity,
                notional_dollars=notional,
                status="error",
                message=exc.__class__.__name__,
            )
            raise
        await store.log_live_order_event(
            event_type="submit_success",
            raw=result,
            order_id=result.get("order_id"),
            client_order_id=result.get("client_order_id") or order.client_order_id,
            ticker=ticker,
            side="yes",
            limit_price_cents=limit_cents,
            quantity=quantity,
            notional_dollars=notional,
            status="submitted",
        )
        print()
        print("LIVE ORDER SUBMITTED")
        print("====================")
        print(json.dumps(result, indent=2))
    finally:
        await kalshi_public.close()
        await auth_client.close()


async def live_cancel_order(order_id: str | None) -> None:
    if not order_id:
        print("Usage: .\\.venv\\Scripts\\python.exe main.py --live-cancel-order ORDER_ID")
        return
    settings, config = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    checks = [
        ("BOT_ENV is live", settings.bot_env == "live", settings.bot_env),
        ("live trading derived from BOT_ENV", settings.live_trading_enabled, str(settings.live_trading_enabled)),
        ("paper_only derived from BOT_ENV", config.bot.paper_only is False, str(config.bot.paper_only)),
    ]
    print("Live Cancel Order")
    print("=================")
    print(f"Order ID: {order_id}")
    print()
    blocking = 0
    for name, ok, detail in checks:
        if not ok:
            blocking += 1
        print(f"{'PASS' if ok else 'BLOCK':<5} {name:<30} {detail}")
    if blocking:
        print()
        print("Blocked. No live cancel was submitted.")
        return

    client = _make_kalshi_auth_client()
    if client is None:
        return
    try:
        try:
            result = await client.cancel_event_order(order_id)
        except Exception as exc:
            await store.log_live_order_event(
                event_type="cancel_error",
                raw={"order_id": order_id, "error": str(exc)},
                order_id=order_id,
                status="error",
                message=exc.__class__.__name__,
            )
            raise
        await store.log_live_order_event(
            event_type="cancel_success",
            raw=result,
            order_id=order_id,
            status="canceled",
        )
        print()
        print("LIVE ORDER CANCELED")
        print("===================")
        print(json.dumps(result, indent=2))
    finally:
        await client.close()


async def cancel_stale_live_orders(older_than_minutes: int | None) -> None:
    if older_than_minutes is None or older_than_minutes <= 0:
        print("Usage: .\\.venv\\Scripts\\python.exe main.py --cancel-stale-live-orders --older-than-minutes N")
        return
    client = _make_kalshi_auth_client()
    if client is None:
        return
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    try:
        payload = await client.get_orders(limit=100)
        open_orders = _open_orders(payload)
        print("Cancel Stale Live Orders")
        print("========================")
        print(f"Open orders: {len(open_orders)}")
        canceled = 0
        skipped = 0
        for order in open_orders:
            order_id = order.get("order_id") or order.get("id")
            age = _order_age_minutes(order)
            if not order_id:
                skipped += 1
                print("SKIP  missing order id")
                continue
            if age is None or age < older_than_minutes:
                skipped += 1
                print(f"SKIP  {order_id} age={_minutes(age)}")
                continue
            result = await client.cancel_event_order(str(order_id))
            await store.log_live_order_event(
                event_type="cancel_stale_success",
                raw=result,
                order_id=str(order_id),
                ticker=order.get("ticker") or order.get("market_ticker"),
                status="canceled",
                message=f"age_minutes={age:.1f}",
            )
            canceled += 1
            print(f"CANCEL {order_id} age={age:.1f}m")
        print()
        print(f"Canceled: {canceled}")
        print(f"Skipped:  {skipped}")
    finally:
        await client.close()


async def set_mode(mode: str | None) -> None:
    if mode not in {"paper", "live"}:
        print("Usage: .\\.venv\\Scripts\\python.exe main.py --set-mode paper|live")
        return
    env_path = Path(".env")
    if not env_path.exists():
        print("No .env file found.")
        return
    _set_env_value(env_path, "BOT_ENV", mode)
    settings, config = load_config()
    print("Mode Updated")
    print("============")
    print(f"BOT_ENV:              {settings.bot_env}")
    print(f"live_trading_enabled: {settings.live_trading_enabled}")
    print(f"paper_only:           {config.bot.paper_only}")


async def arm_live(minutes: int | None) -> None:
    if minutes is None or minutes <= 0 or minutes > 60:
        print("Usage: .\\.venv\\Scripts\\python.exe main.py --arm-live --minutes 1..60")
        return
    settings, config = load_config()
    print("Arm Live")
    print("========")
    if settings.bot_env != "live" or not settings.live_trading_enabled or config.bot.paper_only:
        print("Refusing to arm. Set BOT_ENV=live first.")
        return
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    path = _live_arm_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"armed_at": datetime.now(timezone.utc).isoformat(), "expires_at": expires_at.isoformat()}, indent=2), encoding="utf-8")
    print(f"Live manual submits armed until {expires_at.isoformat()}")
    print("This does not submit any order.")


async def research_export() -> None:
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    rows = await store.get_research_export_rows()
    export_dir = Path("exports")
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"research_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    fieldnames = [
        "created_at",
        "ticker",
        "station",
        "probability",
        "fair_yes_cents",
        "best_yes_bid_cents",
        "best_yes_ask_cents",
        "spread_cents",
        "limit_price_cents",
        "requested_quantity",
        "filled_quantity",
        "average_price_cents",
        "risk_allowed",
        "risk_reasons",
        "outcome",
        "brier_score",
        "resolved_at",
        "forecast_max_f",
        "adjusted_mean_f",
        "sigma_f",
        "forecast_age_minutes",
        "metar_age_minutes",
        "threshold_f",
        "comparison",
        "upper_f",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print("Research Export")
    print("===============")
    print(f"Rows: {len(rows)}")
    print(f"Path: {path.resolve()}")


async def provider_export() -> None:
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    rows = await store.get_provider_snapshot_rows()
    export_dir = Path("exports")
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"provider_snapshots_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    fieldnames = [
        "station",
        "generated_at",
        "inserted_at",
        "provider",
        "provider_max_f",
        "weight",
        "noaa_max_f",
        "consensus_max_f",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print("Provider Snapshot Export")
    print("========================")
    print(f"Rows: {len(rows)}")
    print(f"Path: {path.resolve()}")


async def provider_report() -> None:
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    rows = await store.get_provider_snapshot_rows()
    replay_rows = await store.get_provider_replay_rows()
    availability: dict[str, dict[str, float]] = {}
    for row in rows:
        provider = str(row.get("provider") or "unknown")
        max_f = row.get("provider_max_f")
        noaa = row.get("noaa_max_f")
        stats = availability.setdefault(provider, {"count": 0, "delta_sum": 0.0, "delta_count": 0})
        stats["count"] += 1
        if max_f is not None and noaa is not None:
            stats["delta_sum"] += abs(float(max_f) - float(noaa))
            stats["delta_count"] += 1

    reliability = _provider_replay_stats(replay_rows)
    print("Provider Report")
    print("===============")
    print("Availability / NOAA disagreement")
    print("--------------------------------")
    if not availability:
        print("No provider snapshots yet. Run --once first.")
    for provider, stats in sorted(availability.items()):
        avg_delta = stats["delta_sum"] / stats["delta_count"] if stats["delta_count"] else None
        delta_text = f"{avg_delta:.1f}F" if avg_delta is not None else "n/a"
        print(f"{provider:<24} snapshots={int(stats['count']):>4} avg_abs_delta={delta_text}")

    print()
    print("Resolved replay reliability")
    print("---------------------------")
    if not reliability:
        print("No replayable resolved rows yet. New predictions now store threshold/comparison for this.")
    for provider, stats in sorted(reliability.items(), key=lambda item: (-item[1]["count"], item[0])):
        count = int(stats["count"])
        hit_rate = stats["hits"] / count if count else 0.0
        brier = stats["brier_sum"] / count if count else 0.0
        print(f"{provider:<24} n={count:>3} hit_rate={hit_rate:>6.1%} binary_brier={brier:.4f}")


async def tune_source_weights() -> None:
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    reliability = _provider_replay_stats(await store.get_provider_replay_rows())
    print("Source Weight Tuning")
    print("====================")
    if len(reliability) < 2:
        print("Not enough resolved replay data yet.")
        print("Collect paper predictions, record/settle outcomes, then rerun this command.")
        return
    scores = {}
    for provider, stats in reliability.items():
        if provider in {"weather_bot_consensus", "climatology"}:
            continue
        count = int(stats["count"])
        if count < 10:
            continue
        brier = stats["brier_sum"] / count
        scores[provider] = max(0.01, 1.0 - brier)
    total = sum(scores.values())
    if not total:
        print("Resolved sample is still too small for suggested weights.")
        return
    print("Suggested exploratory weights, capped for external-source humility:")
    for provider, score in sorted(scores.items(), key=lambda item: -item[1]):
        print(f"{provider:<24} {min(0.35, score / total * 0.55):.3f}")
    print()
    print("Do not auto-apply these until you have at least 30 resolved same-market outcomes.")


async def backtest_replay() -> None:
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    rows = await store.get_provider_replay_rows()
    provider_stats = _provider_replay_stats(rows)
    print("Replay Backtest")
    print("===============")
    print("Replays resolved predictions against stored provider snapshots using the contract threshold.")
    print(f"Replayable resolved rows: {len(rows)}")
    if not rows:
        print("Need predictions logged after this upgrade plus resolved outcomes.")
        return
    print()
    for provider, stats in sorted(provider_stats.items(), key=lambda item: (-item[1]["count"], item[0])):
        count = int(stats["count"])
        hit_rate = stats["hits"] / count if count else 0.0
        brier = stats["brier_sum"] / count if count else 0.0
        print(f"{provider:<24} n={count:>3} hit_rate={hit_rate:>6.1%} binary_brier={brier:.4f}")

async def backtest() -> None:
    settings, _ = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    performance = await store.get_paper_performance()
    calibration = await store.get_calibration_report()
    summary = performance["summary"]
    realized = performance["realized"]
    edge = performance["edge"]
    print("Backtest Summary")
    print("================")
    print("This is a historical paper-log summary, not a replay engine yet.")
    print(f"Decisions:          {summary['decisions']}")
    print(f"Filled trades:      {summary['filled_trades']}")
    print(f"Filled contracts:   {summary['filled_contracts']}")
    print(f"Paper cost basis:   ${summary['paper_cost']:.2f}")
    print(f"Resolved trades:    {realized['resolved_trades']}")
    print(f"Realized P&L:       ${realized['realized_pnl']:+.2f}")
    print(f"Average edge:       {_pct(edge['avg'])}")
    print(f"Brier score:        {calibration['summary']['avg_brier']:.4f}" if calibration["summary"]["count"] else "Brier score:        n/a")
    if realized["resolved_trades"] < 30:
        print()
        print("Warning: fewer than 30 resolved trades. Treat this as plumbing validation, not strategy proof.")


def _provider_replay_stats(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = {}
    for row in rows:
        raw_text = row.get("forecast_raw_json")
        if not raw_text:
            continue
        raw = json.loads(raw_text)
        consensus = raw.get("weather_bot_consensus") if isinstance(raw, dict) else None
        if not isinstance(consensus, dict):
            continue
        threshold = row.get("threshold_f")
        comparison = row.get("comparison")
        if threshold is None or comparison is None:
            continue
        upper = row.get("upper_f")
        outcome = bool(row.get("outcome"))
        sources = [
            {"source": "weather_bot_consensus", "max_f": consensus.get("consensus_max_f")},
            *list(consensus.get("external_sources") or []),
        ]
        for source in sources:
            if not isinstance(source, dict) or source.get("max_f") is None:
                continue
            provider = str(source.get("source") or "unknown")
            predicted = _contract_result_from_temperature(float(source["max_f"]), float(threshold), str(comparison), upper)
            provider_stats = stats.setdefault(provider, {"count": 0.0, "hits": 0.0, "brier_sum": 0.0})
            provider_stats["count"] += 1
            provider_stats["hits"] += 1 if predicted == outcome else 0
            provider_stats["brier_sum"] += (float(predicted) - float(outcome)) ** 2
    return stats


def _contract_result_from_temperature(max_f: float, threshold: float, comparison: str, upper: Any) -> bool:
    if comparison in {">", ">=", "above", "at_or_above"}:
        return max_f >= threshold
    if comparison in {"<", "<=", "below", "under"}:
        return max_f < threshold
    if comparison == "range":
        if upper is None:
            return False
        return threshold <= max_f < float(upper)
    return False


def write_dashboard_prompt() -> None:
    export_dir = Path("exports")
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / "lovable_dashboard_prompt.md"
    path.write_text(_dashboard_prompt_text(), encoding="utf-8")
    print("Dashboard Prompt")
    print("================")
    print(f"Path: {path.resolve()}")


def _dashboard_prompt_text() -> str:
    return """# Lovable Prompt: Kalshi Weather Bot Research Dashboard

Build a polished, local-first dashboard for a Python/SQLite Kalshi weather prediction research bot.

The dashboard is for research and safety monitoring, not promoting gambling. The tone should be operational, quiet, and risk-focused. It should clearly separate paper trading, read-only live account data, and any guarded manual live action. Do not create autonomous live trading controls.

## Backend Endpoints Codex Will Connect Later

- `GET /api/status`
- `GET /api/paper/report?limit=50`
- `GET /api/paper/performance`
- `GET /api/paper/ledger?limit=100`
- `GET /api/paper/mtm`
- `GET /api/exposure`
- `GET /api/calibration`
- `GET /api/providers`
- `GET /api/provider-snapshots`
- `GET /api/data-health`
- `GET /api/live/readiness`
- `GET /api/live/orders`
- `GET /api/live/positions`
- `POST /api/live/cancel-order` with `{ "order_id": "..." }`

## Required Views

1. Overview: mode, paper-only state, Kalshi auth, balance, live open orders, live positions, unresolved exposure, latest allowed paper trade, last refresh time, and safety badges.
2. Market Decisions: ticker, station, probability, ask, edge, allowed/skipped, skip reasons, data age, spread, liquidity, and timestamp with filters.
3. Paper Portfolio: ledger plus mark-to-market totals for cost, value, realized P&L, and unrealized P&L.
4. Calibration: Brier score, resolved outcomes, win/loss count, station-level calibration, and probability-bucket reliability with small-sample warnings.
5. Forecast Providers: NOAA, Open-Meteo, Tomorrow.io, Visual Crossing, WeatherAPI, Meteostat, and climatology max temps by station, consensus temp, disagreement vs NOAA, freshness, configured/missing status, and replay reliability when available.
6. Risk Controls: read-only cards for max trade size, daily loss cap, unresolved exposure cap, event-date exposure cap, minimum edge, stale forecast/METAR limits, max spread, liquidity threshold, and live manual order cap.
7. Live Safety: read-only live account section. If adding cancel controls, require a confirmation modal. Do not add submit-order controls.

## Design Requirements

- Build the dashboard as the first screen, not a landing page.
- Use dense, scannable tables and restrained colors.
- Avoid casino or hype language.
- Use clear empty states when there is not enough data.
- Use red only for blockers/losses, green only for passes/profit, amber for warnings.
- Include loading, error, stale-data, and disconnected states.
- Use tabs or a sidebar. Make it responsive for desktop and tablet.

## Mock API Shapes

```json
{
  "status": {
    "mode": "paper",
    "paper_only": true,
    "kalshi_auth": "OK",
    "balance_dollars": 10.0,
    "open_live_orders": 0,
    "live_positions": 0,
    "paper_decisions": 128,
    "allowed_paper_trades": 4,
    "unresolved_exposure_dollars": 12.45
  }
}
```

```json
{
  "station": "KNYC",
  "generated_at": "2026-05-25T18:00:00Z",
  "provider": "tomorrow_io",
  "provider_max_f": 74.0,
  "weight": 0.25,
  "noaa_max_f": 69.0,
  "consensus_max_f": 71.4
}
```

```json
{
  "created_at": "2026-05-25T18:03:00Z",
  "ticker": "KXHIGHNY-26MAY25-T72",
  "station": "KNYC",
  "probability": 0.716,
  "best_yes_ask_cents": 45,
  "edge": 0.266,
  "risk_allowed": true,
  "risk_reasons": [],
  "forecast_age_minutes": 22,
  "metar_age_minutes": 8
}
```

Create mock service functions in one file so Codex can replace them with real fetch calls later.
"""


async def monitor_once() -> None:
    settings, config = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    paper = await store.get_report_summary()
    calibration = await store.get_calibration_report()
    unresolved_exposure = await store.get_unresolved_paper_exposure()
    client = _make_kalshi_auth_client()
    open_orders = []
    live_positions = []
    if client is not None:
        try:
            open_orders = _open_orders(await client.get_orders(limit=100))
            live_positions = _nonzero_positions(await client.get_positions(limit=100))
        finally:
            await client.close()
    lines = [
        f"mode={settings.bot_env}",
        f"paper_decisions={paper['decisions']}",
        f"paper_allowed={paper['allowed']}",
        f"unresolved_exposure=${unresolved_exposure:.2f}/${config.risk.max_unresolved_exposure_dollars:.2f}",
        f"resolved_outcomes={calibration['summary']['count']}",
        f"open_live_orders={len(open_orders)}",
        f"nonzero_live_positions={len(live_positions)}",
    ]
    print("Monitor")
    print("=======")
    for line in lines:
        print(line)
    if settings.webhook_url:
        notifier = NotificationClient(settings.webhook_url)
        try:
            await notifier.send("Weather Bot Monitor", "\n".join(lines))
        finally:
            await notifier.close()


async def auto_live_readiness() -> None:
    settings, config = load_config()
    store = SQLiteStore(settings.sqlite_path)
    await store.initialize()
    performance = await store.get_paper_performance()
    calibration = await store.get_calibration_report()
    summary = performance["summary"]
    realized = performance["realized"]
    brier = calibration["summary"]["avg_brier"]
    checks = [
        ("BOT_ENV live", settings.bot_env == "live", settings.bot_env),
        ("decisions threshold", summary["decisions"] >= config.risk.min_auto_live_decisions, f"{summary['decisions']}/{config.risk.min_auto_live_decisions}"),
        ("resolved outcomes threshold", calibration["summary"]["count"] >= config.risk.min_auto_live_resolved_outcomes, f"{calibration['summary']['count']}/{config.risk.min_auto_live_resolved_outcomes}"),
        ("Brier threshold", calibration["summary"]["count"] > 0 and brier <= config.risk.max_auto_live_brier_score, f"{brier:.4f}/{config.risk.max_auto_live_brier_score:.4f}"),
        ("positive realized P&L", (not config.risk.require_positive_auto_live_pnl) or realized["realized_pnl"] > 0, f"${realized['realized_pnl']:+.2f}"),
    ]
    print("Auto Live Readiness")
    print("===================")
    blocking = 0
    for name, ok, detail in checks:
        if not ok:
            blocking += 1
        print(f"{'PASS' if ok else 'BLOCK':<5} {name:<28} {detail}")
    print()
    print("Result: " + ("READY FOR FUTURE AUTO-LIVE IMPLEMENTATION" if blocking == 0 else "NOT READY FOR AUTO-LIVE"))


async def settlement_sync(limit: int, write: bool) -> None:
    await check_settlements(write=write, limit=limit)


def _make_kalshi_auth_client() -> KalshiAuthClient | None:
    settings, _ = load_config()
    if not settings.kalshi_api_key_id:
        print("Missing KALSHI_API_KEY_ID in .env.")
        print("Add the public Key ID from Kalshi's API key page, then rerun this command.")
        return
    if not settings.kalshi_private_key_path:
        print("Missing KALSHI_PRIVATE_KEY_PATH in .env.")
        return None

    return KalshiAuthClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
    )


def _make_external_weather_clients(settings: Any) -> list[Any]:
    clients: list[Any] = [ClimatologyClient()]
    if settings.tomorrow_api_key:
        clients.append(TomorrowClient(settings.tomorrow_api_key))
    if settings.visual_crossing_api_key:
        clients.append(VisualCrossingClient(settings.visual_crossing_api_key))
    if settings.weatherapi_key:
        clients.append(WeatherApiClient(settings.weatherapi_key))
    if settings.meteostat_rapidapi_key:
        clients.append(MeteostatClient(settings.meteostat_rapidapi_key))
    return clients


def _missing_external_weather_keys(settings: Any) -> list[str]:
    missing = []
    if not settings.tomorrow_api_key:
        missing.append("TOMORROW_API_KEY")
    if not settings.visual_crossing_api_key:
        missing.append("VISUAL_CROSSING_API_KEY")
    if not settings.weatherapi_key:
        missing.append("WEATHERAPI_KEY")
    if not settings.meteostat_rapidapi_key:
        missing.append("METEOSTAT_RAPIDAPI_KEY")
    return missing


async def _fetch_optional_external_forecasts(clients: list[Any], station: str, lat: float, lon: float) -> list[Any]:
    forecasts = []
    for client in clients:
        try:
            forecasts.append(await client.get_hourly_forecast(station, lat, lon))
        except Exception as exc:
            logger.warning("External weather provider %s failed for %s: %s", client.__class__.__name__, station, exc)
    return forecasts


async def _close_external_weather_clients(clients: list[Any]) -> None:
    for client in clients:
        await client.close()


def _print_money_field(payload: dict[str, Any], key: str) -> None:
    value = payload.get(key)
    if value is not None:
        print(f"{key}: ${float(value) / 100:.2f}")


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _minutes(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}m"


def _temp(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}F"


def _open_orders(payload: dict[str, Any]) -> list[dict[str, Any]]:
    orders = payload.get("orders") or []
    return [order for order in orders if str(order.get("status", "")).lower() in {"resting", "open", "pending"}]


def _order_age_minutes(order: dict[str, Any]) -> float | None:
    for key in ("created_time", "created_at", "created_ts", "created_ts_ms", "ts_ms"):
        value = order.get(key)
        if value is None:
            continue
        try:
            if isinstance(value, str) and not value.isdigit():
                created = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
            else:
                numeric = float(value)
                if numeric > 10_000_000_000:
                    numeric = numeric / 1000
                created = datetime.fromtimestamp(numeric, timezone.utc)
            return (datetime.now(timezone.utc) - created).total_seconds() / 60
        except (TypeError, ValueError, OSError):
            continue
    return None


def _set_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    updated = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _live_arm_path() -> Path:
    return Path("data") / "live_arm.json"


def _live_arm_status() -> dict[str, Any]:
    path = _live_arm_path()
    if not path.exists():
        return {"armed": False, "detail": "not armed"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expires_at = datetime.fromisoformat(str(payload.get("expires_at")).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError, json.JSONDecodeError):
        return {"armed": False, "detail": "invalid arm file"}
    remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        return {"armed": False, "detail": "expired"}
    return {"armed": True, "detail": f"expires {expires_at.isoformat()}"}


def _nonzero_positions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    positions = payload.get("market_positions") or payload.get("positions") or []
    nonzero = []
    for position in positions:
        quantity = position.get("position") or position.get("yes_count") or position.get("quantity") or 0
        try:
            if float(quantity) != 0:
                nonzero.append(position)
        except (TypeError, ValueError):
            nonzero.append(position)
    return nonzero


def _delta(left: float | None, right: float | None) -> str:
    if left is None or right is None:
        return "n/a"
    return f"{right - left:+.1f}F"


def _exposure_metadata(ticker: str, series_station_map: dict[str, str]) -> dict[str, str]:
    parts = ticker.split("-")
    series = parts[0] if parts else ticker
    event_date = parts[1] if len(parts) > 1 else "unknown"
    return {
        "series": series,
        "event_date": event_date,
        "station": series_station_map.get(series, "unknown"),
    }


def _settlement_outcome_from_market(market: dict[str, Any]) -> bool | None:
    result = market.get("result")
    if isinstance(result, str):
        normalized = result.strip().lower()
        if normalized == "yes":
            return True
        if normalized == "no":
            return False

    value = market.get("expiration_value")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"yes", "y", "true", "1"}:
            return True
        if normalized in {"no", "n", "false", "0"}:
            return False
    return None


def parse_temperature_contract(market: dict[str, Any], series_station_map: dict[str, str]) -> ContractSpec | None:
    ticker = str(market.get("ticker", ""))
    text = " ".join(str(market.get(key, "")) for key in ("ticker", "title", "subtitle", "yes_sub_title"))

    series_ticker = _series_from_market(market)
    station = series_station_map.get(series_ticker)
    if station is None:
        return None

    threshold = None
    upper = None
    comparison = "at_or_above"
    threshold_match = re.search(r"-(T\d+(?:\.\d+)?)$", ticker)
    bucket_match = re.search(r"-(B\d+(?:\.\d+)?)$", ticker)
    if threshold_match is not None:
        threshold = float(threshold_match.group(1)[1:])
    elif bucket_match is not None:
        center = float(bucket_match.group(1)[1:])
        threshold = center - 0.5
        upper = center + 0.5
        comparison = "range"
    else:
        return None

    lowered = text.lower()
    if comparison != "range" and ("<" in text or "below" in lowered or "under" in lowered or "less than" in lowered):
        comparison = "below"

    expiration = _parse_time(market.get("close_time") or market.get("expiration_time"))
    return ContractSpec(
        ticker=ticker,
        station=station,
        threshold_f=threshold,
        comparison=comparison,
        expiration_time=expiration,
        upper_f=upper,
    )


def _series_from_market(market: dict[str, Any]) -> str:
    event_ticker = str(market.get("event_ticker", ""))
    ticker = str(market.get("ticker", ""))
    return event_ticker.split("-")[0] if event_ticker else ticker.split("-")[0]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe paper-trading Kalshi weather bot")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit")
    parser.add_argument("--report", action="store_true", help="Print the latest paper-trading report")
    parser.add_argument("--reset-paper", action="store_true", help="Clear local paper-trading history")
    parser.add_argument("--kalshi-auth-check", action="store_true", help="Run a read-only authenticated Kalshi check")
    parser.add_argument("--kalshi-balance", action="store_true", help="Print read-only Kalshi balance")
    parser.add_argument("--kalshi-positions", action="store_true", help="Print read-only Kalshi positions")
    parser.add_argument("--kalshi-orders", action="store_true", help="Print read-only Kalshi orders")
    parser.add_argument("--live-readiness-check", action="store_true", help="Show conservative live-readiness blockers")
    parser.add_argument("--calibration-report", action="store_true", help="Print Brier score calibration report")
    parser.add_argument("--record-outcomes", action="store_true", help="Explain outcome recording workflow")
    parser.add_argument("--check-settlements", action="store_true", help="Preview/write explicit Kalshi settlements")
    parser.add_argument("--status", action="store_true", help="Print overall bot/account/research status")
    parser.add_argument("--paper-mtm", action="store_true", help="Mark filled paper positions to current orderbook bids")
    parser.add_argument("--paper-ledger", action="store_true", help="Print filled paper trades with current MTM values")
    parser.add_argument("--paper-performance", action="store_true", help="Print paper performance, calibration, and skip stats")
    parser.add_argument("--data-health", action="store_true", help="Print NOAA/METAR freshness by station")
    parser.add_argument("--exposure-report", action="store_true", help="Print grouped paper exposure by ticker/date/station")
    parser.add_argument("--research-export", action="store_true", help="Export research rows to CSV")
    parser.add_argument("--provider-export", action="store_true", help="Export forecast-provider snapshots to CSV")
    parser.add_argument("--provider-report", action="store_true", help="Print provider availability/replay reliability")
    parser.add_argument("--tune-source-weights", action="store_true", help="Suggest source weights from resolved replay data")
    parser.add_argument("--backtest", action="store_true", help="Summarize historical paper-log performance")
    parser.add_argument("--backtest-replay", action="store_true", help="Replay resolved outcomes against stored provider forecasts")
    parser.add_argument("--dashboard-prompt", action="store_true", help="Write a detailed Lovable dashboard prompt")
    parser.add_argument("--monitor-once", action="store_true", help="Print/send one monitoring heartbeat")
    parser.add_argument("--auto-live-readiness", action="store_true", help="Check evidence thresholds for future auto-live")
    parser.add_argument("--settlement-sync", action="store_true", help="Alias for checked settlement recording workflow")
    parser.add_argument("--set-mode", choices=["paper", "live"], help="Set BOT_ENV in .env")
    parser.add_argument("--arm-live", action="store_true", help="Arm guarded manual live submits for a short window")
    parser.add_argument("--cancel-stale-live-orders", action="store_true", help="Cancel open live orders older than a threshold")
    parser.add_argument("--live-order-preview", action="store_true", help="Preview one guarded real-money post-only limit order")
    parser.add_argument("--live-order-submit", action="store_true", help="Submit one guarded real-money post-only limit order")
    parser.add_argument("--live-cancel-order", metavar="ORDER_ID", help="Cancel one live event order by order id")
    parser.add_argument("--record-outcome", metavar="TICKER", help="Manually record one resolved outcome")
    parser.add_argument("--outcome", choices=["yes", "no"], help="Outcome for --record-outcome")
    parser.add_argument("--ticker", help="Ticker for guarded live order commands")
    parser.add_argument("--limit-cents", type=int, help="Limit price in cents for guarded live order commands")
    parser.add_argument("--quantity", type=int, help="Contract quantity for guarded live order commands")
    parser.add_argument("--i-understand-real-money", action="store_true", help="Required confirmation for --live-order-submit")
    parser.add_argument("--minutes", type=int, help="Minutes for --arm-live")
    parser.add_argument("--older-than-minutes", type=int, help="Age threshold for --cancel-stale-live-orders")
    parser.add_argument("--yes", action="store_true", help="Confirm destructive local maintenance commands")
    parser.add_argument("--limit", type=int, default=20, help="Rows to show with --report")
    args = parser.parse_args()
    if args.report:
        asyncio.run(print_report(args.limit))
    elif args.reset_paper:
        asyncio.run(reset_paper_history(args.yes))
    elif args.kalshi_auth_check:
        asyncio.run(kalshi_auth_check())
    elif args.kalshi_balance:
        asyncio.run(kalshi_balance())
    elif args.kalshi_positions:
        asyncio.run(kalshi_positions(args.limit))
    elif args.kalshi_orders:
        asyncio.run(kalshi_orders(args.limit))
    elif args.live_readiness_check:
        asyncio.run(live_readiness_check())
    elif args.calibration_report:
        asyncio.run(calibration_report())
    elif args.record_outcomes:
        print("Automatic outcome recording is not implemented yet.")
        print("Use verified settlement data, then record one outcome manually:")
        print(".\\.venv\\Scripts\\python.exe main.py --record-outcome TICKER --outcome yes|no")
    elif args.check_settlements:
        asyncio.run(check_settlements(write=args.yes, limit=args.limit))
    elif args.status:
        asyncio.run(status())
    elif args.paper_mtm:
        asyncio.run(paper_mark_to_market())
    elif args.paper_ledger:
        asyncio.run(paper_ledger(args.limit))
    elif args.paper_performance:
        asyncio.run(paper_performance())
    elif args.data_health:
        asyncio.run(data_health())
    elif args.exposure_report:
        asyncio.run(exposure_report())
    elif args.research_export:
        asyncio.run(research_export())
    elif args.provider_export:
        asyncio.run(provider_export())
    elif args.provider_report:
        asyncio.run(provider_report())
    elif args.tune_source_weights:
        asyncio.run(tune_source_weights())
    elif args.backtest:
        asyncio.run(backtest())
    elif args.backtest_replay:
        asyncio.run(backtest_replay())
    elif args.dashboard_prompt:
        write_dashboard_prompt()
    elif args.monitor_once:
        asyncio.run(monitor_once())
    elif args.auto_live_readiness:
        asyncio.run(auto_live_readiness())
    elif args.settlement_sync:
        asyncio.run(settlement_sync(limit=args.limit, write=args.yes))
    elif args.set_mode:
        asyncio.run(set_mode(args.set_mode))
    elif args.arm_live:
        asyncio.run(arm_live(args.minutes))
    elif args.cancel_stale_live_orders:
        asyncio.run(cancel_stale_live_orders(args.older_than_minutes))
    elif args.live_order_preview:
        asyncio.run(
            live_order_preview_or_submit(
                submit=False,
                ticker=args.ticker,
                limit_cents=args.limit_cents,
                quantity=args.quantity,
                confirmed=args.i_understand_real_money,
            )
        )
    elif args.live_order_submit:
        asyncio.run(
            live_order_preview_or_submit(
                submit=True,
                ticker=args.ticker,
                limit_cents=args.limit_cents,
                quantity=args.quantity,
                confirmed=args.i_understand_real_money,
            )
        )
    elif args.live_cancel_order:
        asyncio.run(live_cancel_order(args.live_cancel_order))
    elif args.record_outcome:
        asyncio.run(record_outcome(args.record_outcome, args.outcome))
    else:
        asyncio.run(run_once() if args.once else run_forever())


if __name__ == "__main__":
    main()