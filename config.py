from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class RuntimeSettings:
    kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: str | None = None
    bot_env: str = "paper"
    log_level: str = "INFO"
    sqlite_path: str = "./data/weather_bot.sqlite3"
    config_path: str = "./config.example.yaml"
    robot_api_url: str | None = None
    robot_api_key: str | None = None
    tomorrow_api_key: str | None = None
    visual_crossing_api_key: str | None = None
    weatherapi_key: str | None = None
    meteostat_rapidapi_key: str | None = None
    webhook_url: str | None = None
    live_trading_enabled: bool = False
    live_max_order_dollars: float = 1.0


@dataclass(frozen=True)
class BotConfig:
    poll_seconds: int = 60
    paper_only: bool = True
    supported_stations: list[str] = field(default_factory=list)
    station_points: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class KalshiConfig:
    weather_series_tickers: list[str] = field(default_factory=lambda: ["KXHIGHNY"])
    series_station_map: dict[str, str] = field(default_factory=lambda: {"KXHIGHNY": "KNYC"})
    min_liquidity_contracts: int = 100
    max_spread_cents: int = 8


@dataclass(frozen=True)
class RiskConfig:
    min_edge: float = 0.15
    max_trade_dollars: float = 5.0
    max_daily_loss_dollars: float = 20.0
    max_unresolved_exposure_dollars: float = 15.0
    max_event_date_exposure_dollars: float = 10.0
    max_live_orders_per_day: int = 1
    live_reject_cooldown_minutes: int = 10
    min_auto_live_decisions: int = 100
    min_auto_live_resolved_outcomes: int = 30
    max_auto_live_brier_score: float = 0.25
    require_positive_auto_live_pnl: bool = True
    max_market_exposure_fraction: float = 0.10
    stale_data_minutes: int = 20
    stale_forecast_minutes: int = 120
    stale_metar_minutes: int = 20
    near_expiration_minutes: int = 45
    high_confidence_threshold: float = 0.90


@dataclass(frozen=True)
class ModelConfig:
    base_sigma_f: float = 3.5
    same_day_sigma_f: float = 2.0
    metar_weight: float = 0.20
    open_meteo_weight: float = 0.25
    tomorrow_weight: float = 0.25
    visual_crossing_weight: float = 0.20
    weatherapi_weight: float = 0.15
    meteostat_weight: float = 0.10
    climatology_weight: float = 0.05
    max_external_consensus_weight: float = 0.55
    disagreement_sigma_threshold_f: float = 3.0
    disagreement_sigma_penalty_per_f: float = 0.35
    expiration_decay_strength: float = 0.35


@dataclass(frozen=True)
class AppConfig:
    bot: BotConfig = field(default_factory=BotConfig)
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    model: ModelConfig = field(default_factory=ModelConfig)


def load_config() -> tuple[RuntimeSettings, AppConfig]:
    load_dotenv()
    settings = RuntimeSettings(
        kalshi_base_url=os.getenv("KALSHI_BASE_URL", RuntimeSettings.kalshi_base_url),
        kalshi_api_key_id=_clean_env(os.getenv("KALSHI_API_KEY_ID")),
        kalshi_private_key_path=_clean_env(os.getenv("KALSHI_PRIVATE_KEY_PATH")),
        bot_env=os.getenv("BOT_ENV", RuntimeSettings.bot_env),
        log_level=os.getenv("LOG_LEVEL", RuntimeSettings.log_level),
        sqlite_path=os.getenv("SQLITE_PATH", RuntimeSettings.sqlite_path),
        config_path=os.getenv("CONFIG_PATH", RuntimeSettings.config_path),
        robot_api_url=os.getenv("ROBOT_API_URL") or None,
        robot_api_key=os.getenv("ROBOT_API_KEY") or None,
        tomorrow_api_key=_clean_env(os.getenv("TOMORROW_API_KEY")),
        visual_crossing_api_key=_clean_env(os.getenv("VISUAL_CROSSING_API_KEY")),
        weatherapi_key=_clean_env(os.getenv("WEATHERAPI_KEY")),
        meteostat_rapidapi_key=_clean_env(os.getenv("METEOSTAT_RAPIDAPI_KEY")),
        webhook_url=_clean_env(os.getenv("WEATHER_BOT_WEBHOOK_URL")),
        live_trading_enabled=False,
        live_max_order_dollars=float(os.getenv("LIVE_MAX_ORDER_DOLLARS", "1.0")),
    )
    config_path = Path(settings.config_path)
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = AppConfig(
        bot=BotConfig(**raw.get("bot", {})),
        kalshi=KalshiConfig(**raw.get("kalshi", {})),
        risk=RiskConfig(**raw.get("risk", {})),
        model=ModelConfig(**raw.get("model", {})),
    )
    mode = settings.bot_env.strip().lower()
    if mode == "paper":
        return replace(settings, bot_env="paper", live_trading_enabled=False), replace(
            config, bot=replace(config.bot, paper_only=True)
        )
    if mode == "live":
        return replace(settings, bot_env="live", live_trading_enabled=True), replace(
            config, bot=replace(config.bot, paper_only=False)
        )
    raise RuntimeError("BOT_ENV must be either paper or live.")


def _clean_env(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().strip("\"'")
    return cleaned or None


def _env_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
