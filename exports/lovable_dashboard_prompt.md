# Lovable Prompt: Kalshi Weather Bot Research Dashboard

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
