# Safe Kalshi Weather Paper Bot

Educational Python starter project for a paper-trading Kalshi weather prediction bot focused on daily maximum temperature markets.

This repository is intentionally paper-only. It estimates probabilities from NOAA/NWS forecasts and METAR observations, compares them with Kalshi orderbook prices, applies strict risk gates, and logs simulated limit-order trades to SQLite.

## Architecture

- `data_ingestors/kalshi_client.py`: async Kalshi market and orderbook reads. No live order placement.
- `data_ingestors/noaa_client.py`: async NWS point/grid forecast ingestion from `api.weather.gov`.
- `data_ingestors/metar_client.py`: async METAR observations from Aviation Weather Center.
- `data_ingestors/open_meteo_client.py`: async Open-Meteo forecast ingestion for an independent no-key forecast check.
- `data_ingestors/external_weather_clients.py`: optional Tomorrow.io, Visual Crossing, WeatherAPI, Meteostat, and climatology providers.
- `data_ingestors/notification_client.py`: optional webhook notifications.
- `data_ingestors/robot_api_client.py`: optional async adapter for your private LLM/Chromium bridge using `Authorization: Bearer ...` and a `question` JSON key.
- `engine/model.py`: daily max temperature probability model with staleness and time-to-expiration decay.
- `engine/calibration.py`: Brier score tracking and simple probability calibration hooks.
- `engine/risk_manager.py`: edge, freshness, liquidity, spread, exposure, daily loss, and expiration gates.
- `engine/execution_engine.py`: paper-only limit order simulator with weighted average fill estimates.
- `storage/sqlite_store.py`: SQLite schema, trade history, forecasts, probabilities, and calibration metrics.
- `main.py`: async orchestration loop.

## Recommended APIs

- Kalshi Exchange API v2 for public market data and orderbooks: `GET /markets`, `GET /markets/{ticker}/orderbook`, and multiple orderbooks where useful. See [Kalshi API docs](https://docs.kalshi.com/welcome) and [orderbook docs](https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks).
- National Weather Service API for forecast grids and hourly forecast data. Use `/points/{lat},{lon}` to discover grid links, then hourly forecast/grid data from the returned URLs. See [NWS gridpoint FAQ](https://weather-gov.github.io/api/gridpoints).
- Aviation Weather Center Data API for METAR observations, usually `https://aviationweather.gov/api/data/metar?ids=KJFK&format=json`. See [AWC Data API](https://aviationweather.gov/data/api/).
- Open-Meteo forecast API for independent no-key hourly temperature forecasts. See [Open-Meteo docs](https://open-meteo.com/en/docs).
- Optional provider APIs for broader consensus: Tomorrow.io, Visual Crossing Timeline API, WeatherAPI.com, Meteostat/RapidAPI, and a local climatology baseline.

## Libraries

- `httpx` for async HTTP.
- `tenacity` for retry/backoff.
- `dataclasses`, `PyYAML`, and `python-dotenv` for typed lightweight settings.
- `PyYAML` for config.
- `aiosqlite` for async SQLite.
- Later: `pandas`, `scikit-learn`, and `statsmodels` for richer backtests and calibration once data volume justifies them.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env .env
cp config.example.yaml config.yaml
python main.py --once
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item config.example.yaml config.yaml
python main.py --once
```

## Optional Robot API Adapter

Configure this only in your local `.env`; do not commit a real key:

```dotenv
ROBOT_API_URL=http://100.91.139.29:5000/ask
ROBOT_API_KEY=replace-with-your-key
```

Usage:

```python
from data_ingestors.robot_api_client import RobotApiClient

client = RobotApiClient(api_url, api_key)
answer = await client.ask("Summarize today's weather risk.")
await client.close()
```

## Optional Weather Providers And Alerts

Add any keys you have to `.env`; missing keys are skipped safely:

```dotenv
TOMORROW_API_KEY=
VISUAL_CROSSING_API_KEY=
WEATHERAPI_KEY=
METEOSTAT_RAPIDAPI_KEY=
WEATHER_BOT_WEBHOOK_URL=
```

Without these keys, the bot still uses NOAA, METAR, Open-Meteo, and local climatology.

Consensus weights are source-specific:

```yaml
model:
  open_meteo_weight: 0.25
  tomorrow_weight: 0.25
  visual_crossing_weight: 0.20
  weatherapi_weight: 0.15
  meteostat_weight: 0.10
  climatology_weight: 0.05
  max_external_consensus_weight: 0.55
```

Climatology is intentionally light and is ignored by the short-term forecast disagreement penalty.

## Monitoring And Deployment

One-shot monitor:

```powershell
.\.venv\Scripts\python.exe main.py --monitor-once
```

Future auto-live evidence gate:

```powershell
.\.venv\Scripts\python.exe main.py --auto-live-readiness
```

Ubuntu systemd templates live in `deploy/`:

- `weather-bot.service.example`
- `weather-bot-monitor.service.example`
- `weather-bot-monitor.timer.example`

## Database Schema

SQLite tables are created on startup:

- `forecasts`: forecast snapshots by station/date/source.
- `observations`: METAR snapshots by station.
- `probabilities`: model probabilities and inputs.
- `paper_trades`: simulated trade intents/fills.
- `market_snapshots`: Kalshi price and depth snapshots.
- `calibration_metrics`: realized outcomes and Brier scores.

## Example Trade Flow

1. Fetch active Kalshi markets whose event ticker matches configured weather prefixes.
2. Parse supported station and threshold metadata from market titles/tickers when available.
3. Fetch NWS hourly forecast and latest METAR for the station.
4. Estimate probability that daily max temperature satisfies the contract.
5. Convert best executable YES/NO ask to implied probability.
6. Compute `edge = model_probability - market_implied_probability`.
7. Run risk gates: minimum edge, freshness, orderbook depth, spread, exposure, daily loss, expiration proximity, and halt status.
8. Submit a paper limit order only; estimate fill using visible book depth.
9. Log forecast, probability, market snapshot, risk decision, and simulated trade.

## Supported Weather Cities

Configured stations:

- `KNYC`: New York / Central Park area
- `KORD`: Chicago
- `KDEN`: Denver
- `KLAX`: Los Angeles
- `KATL`: Atlanta
- `KMCI`: Kansas City

Configured Kalshi high-temperature series:

- `KXHIGHNY -> KNYC`
- `KXHIGHCHI -> KORD`
- `KXHIGHDEN -> KDEN`
- `KXHIGHLAX -> KLAX`
- `KXHIGHTATL -> KATL`

Kansas City is configured for weather diagnostics and consensus checks, but no active Kalshi daily high-temperature series was found for Kansas City at implementation time.

Because additional series increase public API traffic, Kalshi market scanning uses retry/backoff and a short pause between series requests.

## Probability Model Ideas

- Start with an interpretable normal model around a NOAA/Open-Meteo consensus hourly max forecast.
- Use same-day tighter uncertainty and multi-day wider uncertainty.
- Blend latest METAR temperature trend into the forecast mean.
- Add time-to-expiration decay so late markets require stronger confidence.
- Penalize or widen uncertainty when NOAA and external forecast sources disagree sharply.
- Track Brier score by station, lead time, and threshold bucket.
- Later, fit calibration curves using isotonic regression or Platt scaling once enough outcomes exist.

## Backtesting Ideas

- Replay saved NWS forecasts, METARs, Kalshi orderbooks, and market outcomes by timestamp.
- Measure Brier score, expected value, realized P&L, drawdown, fill quality, skipped-trade reasons, and edge decay.
- Separate train/calibration/evaluation periods by date to avoid leakage.
- Include conservative fill assumptions and fees before considering live trading.

## Monitoring

- Structured logs to stdout for Ubuntu/systemd.
- SQLite audit trail for every decision, including rejected trades.
- Add Prometheus or healthcheck endpoints later for long-running deployment.
- Alert on stale data, API failures, abnormal volatility, and daily loss lockouts.

## Reset Paper History

To clear local simulated trades and runtime snapshots:

```powershell
.\.venv\Scripts\python.exe main.py --reset-paper --yes
```

This does not delete `.env`, API keys, source code, or configuration.

## Kalshi Auth Check

After setting `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`, verify read-only auth:

```powershell
.\.venv\Scripts\python.exe main.py --kalshi-auth-check
```

This calls `GET /api_keys` only. It does not place, amend, or cancel orders.

Read-only portfolio checks:

```powershell
.\.venv\Scripts\python.exe main.py --kalshi-balance
.\.venv\Scripts\python.exe main.py --kalshi-positions --limit 20
.\.venv\Scripts\python.exe main.py --kalshi-orders --limit 20
```

Conservative readiness checklist:

```powershell
.\.venv\Scripts\python.exe main.py --live-readiness-check
```

This does not enable autonomous live trading.

## Guarded Live Smoke Test

Autonomous live trading is still disabled. For a tiny real-money smoke test, use a manual post-only YES bid.

Preview first while still in paper mode:

```powershell
.\.venv\Scripts\python.exe main.py --live-order-preview --ticker KXHIGHNY-26MAY25-T72 --limit-cents 1 --quantity 1
```

To submit, all of these must be true:

- `.env` has `BOT_ENV=live`
- `.env` has `LIVE_MAX_ORDER_DOLLARS=1.00`
- command includes `--i-understand-real-money`
- notional is within the live max and configured trade cap
- order is post-only and priced below the current YES ask
- no live positions or open live orders exist
- live submits are armed with `--arm-live`
- daily live order count is below `max_live_orders_per_day`

Submit example:

```powershell
.\.venv\Scripts\python.exe main.py --arm-live --minutes 30
.\.venv\Scripts\python.exe main.py --live-order-submit --ticker KXHIGHNY-26MAY25-T72 --limit-cents 1 --quantity 1 --i-understand-real-money
```

The manual live command uses Kalshi's V2 event order endpoint, `POST /portfolio/events/orders`, and sends `post_only: true`, `time_in_force: good_till_canceled`, and `self_trade_prevention_type: taker_at_cross`.

Mode switching is intentionally one setting:

```dotenv
BOT_ENV=paper
```

or:

```dotenv
BOT_ENV=live
```

`bot.paper_only` and live trading guards are derived from `BOT_ENV`, so you do not need to edit YAML to switch modes.

Cancel a resting live order:

```powershell
.\.venv\Scripts\python.exe main.py --kalshi-orders --limit 10
.\.venv\Scripts\python.exe main.py --live-cancel-order ORDER_ID
```

The cancel command uses Kalshi's V2 event cancel endpoint, `DELETE /portfolio/events/orders/{order_id}`.

Cancel stale resting live orders:

```powershell
.\.venv\Scripts\python.exe main.py --cancel-stale-live-orders --older-than-minutes 10
```

All live previews, submit attempts, submit successes/errors, and cancels are logged to SQLite in `live_order_events`.

## Example AI Review Prompt

Use this kind of prompt for your private LLM/robot layer when you want a human-readable review of a paper candidate:

```text
You are a cautious quantitative risk reviewer for a PAPER-ONLY Kalshi weather bot.

Review this candidate trade. Do not encourage gambling, do not suggest increasing size,
and do not override risk controls. Explain whether the bot should skip or paper-trade
based only on the provided data.

Market: KXHIGHNY-26MAY25-T72
Contract: NYC daily high temperature below 72 F
Model probability: 0.716
Market executable YES ask: 54 cents
Computed edge: 0.176
Forecast max: 70 F
Forecast age: 8 minutes
METAR age: 11 minutes
Orderbook visible depth: 1200 contracts
Spread: 4 cents
Daily paper P&L: -4.86 dollars
Max paper trade size: 5 dollars
Max daily paper loss: 20 dollars
Mode: PAPER ONLY

Return:
1. Decision: PAPER_TRADE or SKIP
2. Main reason
3. Risk flags
4. One-sentence plain-English explanation
```

## Calibration

Print calibration metrics:

```powershell
.\.venv\Scripts\python.exe main.py --calibration-report
```

Manual outcome recording, after verifying settlement:

```powershell
.\.venv\Scripts\python.exe main.py --record-outcome KXHIGHNY-26MAY25-T72 --outcome yes
```

Automatic outcome recording is intentionally not implemented until an auditable settlement source is wired in.

Preview explicit Kalshi settlement fields:

```powershell
.\.venv\Scripts\python.exe main.py --check-settlements
```

Write only explicit resolved outcomes:

```powershell
.\.venv\Scripts\python.exe main.py --check-settlements --yes
```

## Status

Print a one-screen bot summary:

```powershell
.\.venv\Scripts\python.exe main.py --status
```

## Paper Mark-To-Market

Estimate paper position value using current YES bids:

```powershell
.\.venv\Scripts\python.exe main.py --paper-mtm
```

Print a filled-trade ledger with per-trade mark-to-market:

```powershell
.\.venv\Scripts\python.exe main.py --paper-ledger --limit 20
```

Print paper performance, calibration, and skip-reason stats:

```powershell
.\.venv\Scripts\python.exe main.py --paper-performance
```

Check NOAA and METAR freshness by station:

```powershell
.\.venv\Scripts\python.exe main.py --data-health
```

Freshness is split by source in `config.example.yaml`:

```yaml
risk:
  stale_forecast_minutes: 120
  stale_metar_minutes: 20
```

Risk rejections use `stale_forecast`, `stale_metar`, or `missing_metar` so data quality issues are easier to diagnose.

Forecast provider diagnostics:

```powershell
.\.venv\Scripts\python.exe main.py --provider-report
.\.venv\Scripts\python.exe main.py --provider-export
.\.venv\Scripts\python.exe main.py --backtest-replay
.\.venv\Scripts\python.exe main.py --tune-source-weights
```

`--provider-report` shows provider availability and disagreement vs NOAA. Once newer predictions have stored threshold/comparison metadata and outcomes are recorded, `--backtest-replay` and `--tune-source-weights` use those resolved rows to evaluate provider reliability. Treat the tuning output as research until at least 30 resolved same-market outcomes exist.

Create the Lovable dashboard build prompt:

```powershell
.\.venv\Scripts\python.exe main.py --dashboard-prompt
```

The prompt is written to `exports/lovable_dashboard_prompt.md`.

Print grouped unresolved paper exposure:

```powershell
.\.venv\Scripts\python.exe main.py --exposure-report
```

Unresolved paper exposure is capped in `config.example.yaml`:

```yaml
risk:
  max_unresolved_exposure_dollars: 15.00
  max_event_date_exposure_dollars: 10.00
```

Resolved tickers with recorded outcomes no longer consume that exposure budget.
The event-date cap separately limits concentration in one date such as `26MAY25`.

## Safety Notes

- This code never sends live orders.
- `BOT_ENV` is the single switch between paper and guarded manual live mode.
- Market orders are not represented in the execution layer.
- Martingale sizing is intentionally absent.
